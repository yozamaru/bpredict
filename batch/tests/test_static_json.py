"""静的JSON の組み立てと書き出し（詳細設計 3.7）。

**キー構造は `contracts/public-shapes.json` との完全一致で検査する。** 書き出す側は
Python、API は TypeScript で同じコードを共有できないため、キー構造だけを1つの
ファイルに固定し、両方のテストがそれを読む。片方だけを直すと必ず落ちる。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from batch.static_json.builder import (
    AccuracyInput,
    ActualInput,
    ClubInput,
    EvaluationInput,
    GameDetailInput,
    GameInput,
    GameListInput,
    GameListRow,
    MetaInput,
    PlayerInput,
    PredictionInput,
    ReasonInput,
    build_game_detail,
    build_game_list,
    build_meta,
)
from batch.static_json.writer import (
    GAME_INDEX,
    MAX_DATA_FILES,
    SCHEDULE_WINDOW_DAYS,
    carry_forward_last_success,
    carry_forward_latest_result,
    count_data_files,
    read_game_index,
    write_static_json,
)

CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "public-shapes.json"

GENERATED_AT = "2026-09-26T21:02:00Z"


def key_paths(value: Any, prefix: str = "") -> set[str]:
    """キーのパスを集める。オブジェクトは '.'、配列は '[]' を挟む（詳細設計 3.7）。

    **値は見ない。** 値の範囲は他のテストが個別に見ており、ここで二重に持つと同じ
    検査が2箇所に散る。
    """
    if isinstance(value, dict):
        paths: set[str] = set()
        for key, child in value.items():
            paths |= key_paths(child, f"{prefix}.{key}" if prefix else key)
        return paths
    if isinstance(value, list):
        if not value:
            return {f"{prefix}[]"}
        paths = set()
        for child in value:
            paths |= key_paths(child, f"{prefix}[]")
        return paths
    return {prefix}


def contract(shape: str) -> set[str]:
    loaded = json.loads(CONTRACT.read_text(encoding="utf-8"))
    return set(loaded[shape]["paths"])


def a_club(suffix: str) -> ClubInput:
    return ClubInput(
        club_id=f"c{suffix}",
        slug=f"club-{suffix}",
        name=f"架空クラブ{suffix}",
        short_name=f"架空{suffix}",
    )


def a_game(**over: Any) -> GameInput:
    defaults: dict[str, Any] = {
        "game_id": "g1",
        "tipoff_at": "2026-09-26T10:05:00Z",
        "status": "SCHEDULED",
        "competition": "REGULAR",
        "home": a_club("h"),
        "away": a_club("a"),
        "league": "PREMIER",
        "venue_name": "架空アリーナ",
    }
    defaults.update(over)
    return GameInput(**defaults)


def a_prediction(**over: Any) -> PredictionInput:
    defaults: dict[str, Any] = {
        "home_win_prob": 0.68,
        "pred_home_score": 84,
        "pred_away_score": 78,
        "is_provisional": False,
        "is_final": False,
        "model_version": "winner-v1.0.0",
        "is_early_season": False,
    }
    defaults.update(over)
    return PredictionInput(**defaults)


def a_player(**over: Any) -> PlayerInput:
    defaults: dict[str, Any] = {
        "player_id": "p1",
        "name": "架空選手",
        "club_id": "ch",
        "position": "PG",
        "avail_prob": 0.95,
        "minutes": 31.2,
        "fg2a": 7.8,
        "fg3a": 5.3,
        "fta": 3.9,
        "fg2_pct": 0.526,
        "fg3_pct": 0.434,
        "ft_pct": 0.846,
        "oreb": 0.6,
        "dreb": 2.5,
        "ast": 6.1,
        "tov": 2.2,
        "stl": 1.1,
        "blk": 0.3,
        "pf": 2.4,
        "fd": 3.1,
        "err_minutes": 5.8,
        "err_pts": 4.8,
        "err_reb": 2.1,
        "err_ast": 1.5,
    }
    defaults.update(over)
    return PlayerInput(**defaults)


def an_actual(**over: Any) -> ActualInput:
    """実績1人ぶん。**すべてのキーが埋まった標本**（契約の照合に使う）。"""
    defaults: dict[str, Any] = {
        "player_id": "p1",
        "name": "架空選手",
        "club_id": "ch",
        "position": "PG",
        "started": True,
        "minutes": 31.5,
        "fg2m": 4, "fg2a": 8,
        "fg3m": 2, "fg3a": 5,
        "ftm": 4, "fta": 4,
        "oreb": 0, "dreb": 3,
        "ast": 6, "tov": 2, "stl": 1, "blk": 0,
        "pf": 2, "fd": 3,
        "plus_minus": 5,
        "pts": 18,
    }
    defaults.update(over)
    return ActualInput(**defaults)


def a_full_detail() -> GameDetailInput:
    """すべてのキーが埋まった標本。**契約との完全一致はこれで見る。**"""
    return GameDetailInput(
        game=a_game(status="FINISHED", home_score=88, away_score=81),
        prediction=a_prediction(is_final=True),
        reasons=[
            ReasonInput("TEAM_STRENGTH", "チーム力の差", "＋82ポイント", "HOME", 0.42),
            ReasonInput("SCHEDULE", "アウェイの休養", "中0日", "HOME", 0.11),
        ],
        players=[a_player()],
        actuals=[an_actual()],
        evaluation=EvaluationInput(
            is_correct=True,
            score_error=3.0,
            outcome="WIN",
            bucket="60-70%",
            bucket_n=42,
            bucket_rate=0.69,
        ),
        model_accuracy=AccuracyInput(accuracy=0.682, brier=0.204, n=312),
    )


class TestListResults:
    """一覧の実績の併記（詳細設計 3.3 の v1.131）。

    運営者の指摘「詳細画面じゃないと結果が分からない」に対して、**一覧でも
    予測と実績を併記する**。
    """

    def _list(self, row: GameListRow) -> dict[str, Any]:
        payload = build_game_list(
            GameListInput(game_date="2026-10-07", games=[row]), GENERATED_AT)
        games = payload["data"]["games"]
        assert isinstance(games, list)
        first = games[0]
        assert isinstance(first, dict)
        return first

    def test_finished_games_carry_the_score(self) -> None:
        game = self._list(GameListRow(
            a_game(status="FINISHED", home_score=81, away_score=87), a_prediction()))
        assert (game["homeScore"], game["awayScore"]) == (81, 87)

    def test_unfinished_games_hide_the_score(self) -> None:
        """**列に値があっても出さない。** 詳細と同じ関門である（3.3）。"""
        game = self._list(GameListRow(
            a_game(home_score=40, away_score=38), a_prediction()))
        assert (game["homeScore"], game["awayScore"]) == (None, None)

    def test_the_verdict_is_null_until_the_match_is_evaluated(self) -> None:
        """**終了してもすぐには付かない**（freeze は毎時、照合は日次）。

        **false で埋めない** — null は「まだ照合していない」であって
        「外した」ではない。
        """
        game = self._list(GameListRow(
            a_game(status="FINISHED", home_score=81, away_score=87), a_prediction()))
        assert game["evaluation"] is None

    def test_the_verdict_comes_from_the_evaluation(self) -> None:
        game = self._list(GameListRow(
            a_game(status="FINISHED", home_score=81, away_score=87),
            a_prediction(),
            EvaluationInput(is_correct=False, score_error=5.5, outcome="LOSS"),
        ))
        assert game["evaluation"] == {"isCorrect": False, "scoreError": 5.5}

    def test_the_keys_exist_even_before_the_game(self) -> None:
        """**キーを落とさない**（3.1 の契約）。中身だけが変わる。"""
        game = self._list(GameListRow(a_game(), a_prediction()))
        for key in ("homeScore", "awayScore", "evaluation"):
            assert key in game

    def test_a_game_without_a_prediction_has_no_verdict(self) -> None:
        """**予測が無ければ照合も無い**（照合は予測に対する判定である）。

        **ここでも落とす。** 呼び出し側の取りこぼしで「判定する対象の無い判定」が
        出ないようにするためで、`avail_prob` の二重の絞り込みと同じ作法である。
        """
        game = self._list(GameListRow(
            a_game(status="FINISHED", home_score=81, away_score=87),
            None,
            EvaluationInput(is_correct=True, score_error=1.0, outcome="WIN"),
        ))
        assert game["evaluation"] is None
        # スコアは出す（事実であり、予測とは独立している）
        assert (game["homeScore"], game["awayScore"]) == (81, 87)


class TestContract:
    def test_game_list_matches_contract(self) -> None:
        payload = build_game_list(
            GameListInput(
                game_date="2026-09-26",
                games=[GameListRow(
                    a_game(status="FINISHED", home_score=88, away_score=81),
                    a_prediction(),
                    # **実績と判定も入れた標本で契約を照合する**（v1.131）。
                    # 入れないと `evaluation` が null になり、配下のキーが欠ける
                    EvaluationInput(is_correct=True, score_error=3.0, outcome="WIN"),
                )],
                accuracy=AccuracyInput(accuracy=0.682, brier=0.204, n=312),
            ),
            GENERATED_AT,
        )
        assert key_paths(payload) == contract("gamesByDate")

    def test_game_detail_matches_contract(self) -> None:
        assert key_paths(build_game_detail(a_full_detail(), GENERATED_AT)) == contract(
            "gameDetail"
        )

    def test_meta_matches_contract(self) -> None:
        payload = build_meta(
            MetaInput(
                generated_at=GENERATED_AT,
                data_as_of="2026-09-25T12:00:00Z",
                last_run_status="SUCCESS",
                last_success_at=GENERATED_AT,
                model_versions=["winner-v1.0.0"],
            )
        )
        assert key_paths(payload) == contract("meta")


class TestShape:
    def test_prediction_is_null_but_the_key_remains(self) -> None:
        """予測がない試合を一覧から落とさない（要件 8.5）。"""
        payload = build_game_list(
            GameListInput(game_date="2026-09-26", games=[GameListRow(a_game(), None)]), GENERATED_AT
        )
        game = payload["data"]["games"][0]
        assert "prediction" in game
        assert game["prediction"] is None

    def test_keys_do_not_move_between_before_and_after(self) -> None:
        """**キーの位置を試合の状態で動かさない**（詳細設計 3.3）。"""
        before = build_game_detail(
            GameDetailInput(
                game=a_game(),
                prediction=a_prediction(),
                reasons=a_full_detail().reasons,
                players=[a_player()],
                model_accuracy=AccuracyInput(0.682, 0.204, 312),
            ),
            GENERATED_AT,
        )
        after = build_game_detail(a_full_detail(), GENERATED_AT)
        # 試合前は evaluation が null、実績が空配列なので**その下だけ**が減る。
        # それ以外のキーは1つも動かない（動いたらここで落ちる）
        assert key_paths(before) | {
            path for path in key_paths(after)
            if path.startswith(("data.evaluation.", "data.playerActuals[]."))
        } == key_paths(after) | {"data.evaluation", "data.playerActuals[]"}

    def test_actuals_key_exists_before_the_game(self) -> None:
        """**試合前も `playerActuals` のキーを落とさない**（詳細設計 3.3 / 3.1）。

        `key_paths` は空の配列を見ないため、上の検査では捕まらない。
        """
        payload = build_game_detail(
            GameDetailInput(game=a_game(), prediction=a_prediction()), GENERATED_AT,
        )
        assert payload["data"]["playerActuals"] == []

    def test_actuals_do_not_depend_on_the_prediction(self) -> None:
        """**予測が1本も無い試合でも実績を出す**（詳細設計 3.3）。

        2026-10-09 時点の本番11試合すべてがこの状態である。依存させると
        「この試合の記録」が画面に何も出ない。
        """
        payload = build_game_detail(
            GameDetailInput(
                game=a_game(status="FINISHED", home_score=88, away_score=81),
                prediction=None,
                actuals=[an_actual()],
            ),
            GENERATED_AT,
        )
        assert payload["data"]["prediction"] is None
        assert len(payload["data"]["playerActuals"]) == 1
        assert payload["data"]["playerActuals"][0]["summary"]["pts"] == 18

    def test_actual_rates_have_no_threshold(self) -> None:
        """**実績の率に閾値を設けない**（要件 8.3 / 詳細設計 3.3）。

        予測側は `fg3.a >= 3` を課すが、実績の `1 / 2` は丸めのない事実である。
        **試投数が0のときだけ出さない。**
        """
        rows = build_game_detail(
            GameDetailInput(
                game=a_game(status="FINISHED"),
                actuals=[an_actual(fg3m=1, fg3a=2, ftm=0, fta=0)],
            ),
            GENERATED_AT,
        )["data"]["playerActuals"]
        assert rows[0]["box"]["fg3"]["pct"] == 0.5
        assert rows[0]["box"]["ft"]["pct"] is None

    def test_actual_counts_keep_null(self) -> None:
        """**欠損を 0 に置換しない**（規約5）。旧年度は `plus_minus` が無い（4.4）。"""
        rows = build_game_detail(
            GameDetailInput(
                game=a_game(status="FINISHED"),
                actuals=[an_actual(plus_minus=None, oreb=None)],
            ),
            GENERATED_AT,
        )["data"]["playerActuals"]
        assert rows[0]["box"]["plusMinus"] is None
        # 片方が None なら合計も None（0 として足さない）
        assert rows[0]["summary"]["reb"] is None

    def test_actuals_carry_no_error_column(self) -> None:
        """**実績に誤差はない**（詳細設計 3.3）。予測側の `error` を持たない。"""
        rows = build_game_detail(
            GameDetailInput(game=a_game(status="FINISHED"), actuals=[an_actual()]),
            GENERATED_AT,
        )["data"]["playerActuals"]
        assert "error" not in rows[0]

    def test_tipoff_is_utc_and_no_jst_label(self) -> None:
        """UTC のまま出す。JST の文字列を作らない（CLAUDE.md 時刻の扱い）。"""
        payload = build_game_list(
            GameListInput(game_date="2026-09-26", games=[GameListRow(a_game(), a_prediction())]),
            GENERATED_AT,
        )
        assert payload["data"]["games"][0]["tipoffAt"] == "2026-09-26T10:05:00Z"
        assert "tipoffLabel" not in json.dumps(payload)

    def test_away_win_prob_is_not_included(self) -> None:
        """`1 - homeWinProb` の冗長値を持たない（詳細設計 1.5）。"""
        payload = build_game_detail(a_full_detail(), GENERATED_AT)
        assert "awayWinProb" not in json.dumps(payload)

    def test_scores_are_hidden_before_the_game(self) -> None:
        payload = build_game_detail(
            GameDetailInput(game=a_game(home_score=88, away_score=81)), GENERATED_AT
        )
        assert payload["data"]["game"]["homeScore"] is None

    def test_meta_has_no_stale_flag(self) -> None:
        """**`stale` を真偽値で入れない**（詳細設計 3.7）。判定はクライアントが行う。"""
        payload = build_meta(
            MetaInput(GENERATED_AT, None, "SUCCESS", GENERATED_AT, ["winner-v1.0.0"])
        )
        assert "stale" not in json.dumps(payload)

    def test_envelope_meta_holds_only_generated_at(self) -> None:
        """公開APIの `meta` と同じ形に保つ（詳細設計 3.1）。"""
        payload = build_game_list(GameListInput(game_date="2026-09-26"), GENERATED_AT)
        assert payload["meta"] == {"generatedAt": GENERATED_AT}


class TestDerived:
    def test_identities_hold(self) -> None:
        """`FGM = 2FGM + 3FGM`、`PTS = 2FGM×2 + 3FGM×3 + FTM`（A-15）。"""
        box = build_game_detail(a_full_detail(), GENERATED_AT)["data"][
            "playerPredictions"
        ][0]["box"]
        assert box["fg"]["m"] == pytest.approx(box["fg2"]["m"] + box["fg3"]["m"])
        assert box["fg"]["a"] == pytest.approx(box["fg2"]["a"] + box["fg3"]["a"])
        pts = box["fg2"]["m"] * 2 + box["fg3"]["m"] * 3 + box["ft"]["m"]
        summary = build_game_detail(a_full_detail(), GENERATED_AT)["data"][
            "playerPredictions"
        ][0]["summary"]
        assert summary["pts"] == pytest.approx(pts)
        assert summary["reb"] == pytest.approx(box["oreb"] + box["dreb"])

    def test_made_never_exceeds_attempted(self) -> None:
        """成功数は率 × 試投数の導出値であり、構造的に成立する（要件 6.8.5）。"""
        box = build_game_detail(a_full_detail(), GENERATED_AT)["data"][
            "playerPredictions"
        ][0]["box"]
        for key in ("fg", "fg2", "fg3", "ft"):
            assert box[key]["m"] <= box[key]["a"] + 1e-9

    @pytest.mark.parametrize(
        ("key", "attempts"),
        [("fg2", {"fg2a": 2.0}), ("fg3", {"fg3a": 2.0}), ("ft", {"fta": 2.0})],
    )
    def test_pct_is_hidden_below_the_threshold(
        self, key: str, attempts: dict[str, float]
    ) -> None:
        """試投数が閾値未満なら率を出さない（A-17）。分数だけを出す。"""
        detail = GameDetailInput(
            game=a_game(), prediction=a_prediction(), players=[a_player(**attempts)]
        )
        box = build_game_detail(detail, GENERATED_AT)["data"]["playerPredictions"][0][
            "box"
        ]
        assert box[key]["pct"] is None
        assert box[key]["a"] == pytest.approx(2.0)

    def test_fg_pct_hidden_when_total_attempts_are_low(self) -> None:
        detail = GameDetailInput(
            game=a_game(),
            prediction=a_prediction(),
            players=[a_player(fg2a=1.0, fg3a=1.0)],
        )
        box = build_game_detail(detail, GENERATED_AT)["data"]["playerPredictions"][0][
            "box"
        ]
        assert box["fg"]["pct"] is None
        assert box["efgPct"] is None
        assert box["tsPct"] is None

    def test_players_below_avail_threshold_are_excluded(self) -> None:
        """`P(出場) < 0.5` の選手は表示しない（要件 6.8.4）。"""
        detail = GameDetailInput(
            game=a_game(),
            prediction=a_prediction(),
            players=[a_player(avail_prob=0.49), a_player(player_id="p2", avail_prob=0.5)],
        )
        players = build_game_detail(detail, GENERATED_AT)["data"]["playerPredictions"]
        assert [p["playerId"] for p in players] == ["p2"]

    def test_strength_is_a_step_value_not_the_shap_value(self) -> None:
        """SHAP の生値を出さない（詳細設計 2.7）。"""
        reasons = build_game_detail(a_full_detail(), GENERATED_AT)["data"]["prediction"][
            "reasons"
        ]
        assert [r["strength"] for r in reasons] == [4, 2]  # 0.11 / 0.42 → ceil(1.05)
        assert "contribution" not in json.dumps(reasons)


class TestWriter:
    def _write(self, root: Path, **over: Any) -> Any:
        kwargs: dict[str, Any] = {
            "today": GameListInput(
                game_date="2026-09-26", games=[GameListRow(a_game(), a_prediction())]
            ),
            "upcoming": [GameListInput(game_date="2026-09-27")],
            "details": [
                GameDetailInput(game=a_game(), prediction=a_prediction())
            ],
            "generated_at": GENERATED_AT,
            "data_as_of": "2026-09-25T12:00:00Z",
            "last_run_status": "SUCCESS",
            "model_versions": ["winner-v1.0.0"],
            "root": root,
        }
        kwargs.update(over)
        return write_static_json(**kwargs)

    def test_writes_the_documented_file_layout(self, tmp_path: Path) -> None:
        result = self._write(tmp_path)
        assert sorted(result.written) == [
            "games/g1.json",
            f"games/{GAME_INDEX}",
            "meta.json",
            "schedule/2026-09-27.json",
            "today.json",
        ]
        assert (tmp_path / "today.json").exists()

    def test_files_leaving_the_window_are_removed(self, tmp_path: Path) -> None:
        """**窓から出たファイルを削除する**（詳細設計 3.7）。残すと単調に増える。"""
        (tmp_path / "games").mkdir()
        (tmp_path / "schedule").mkdir()
        (tmp_path / "games" / "old.json").write_text("{}", encoding="utf-8")
        (tmp_path / "schedule" / "2026-09-20.json").write_text("{}", encoding="utf-8")

        result = self._write(tmp_path)
        assert sorted(result.removed) == ["games/old.json", "schedule/2026-09-20.json"]
        assert not (tmp_path / "games" / "old.json").exists()

    def test_rewriting_is_idempotent(self, tmp_path: Path) -> None:
        """毎回窓の全ファイルを書き直す。2回流して内容が同じであること。"""
        self._write(tmp_path)
        first = {
            path.relative_to(tmp_path).as_posix(): path.read_text(encoding="utf-8")
            for path in sorted(tmp_path.rglob("*.json"))
        }
        self._write(tmp_path)
        second = {
            path.relative_to(tmp_path).as_posix(): path.read_text(encoding="utf-8")
            for path in sorted(tmp_path.rglob("*.json"))
        }
        assert first == second

    def test_last_success_is_carried_forward_when_not_success(self, tmp_path: Path) -> None:
        """`PARTIAL` の回はこの回の時刻を書けない（詳細設計 3.7）。"""
        self._write(tmp_path)
        later = "2026-09-27T21:02:00Z"
        self._write(tmp_path, generated_at=later, last_run_status="PARTIAL")

        meta = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))["data"]
        # 「いつ書いたか」と「いつ最後に成功したか」を1つに畳まない（基本設計 4.5）
        assert meta["generatedAt"] == later
        assert meta["lastSuccessAt"] == GENERATED_AT
        assert meta["lastRunStatus"] == "PARTIAL"

    def test_last_success_is_null_on_the_first_failed_run(self, tmp_path: Path) -> None:
        self._write(tmp_path, last_run_status="FAILED")
        meta = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))["data"]
        assert meta["lastSuccessAt"] is None

    def test_latest_result_date_is_written(self, tmp_path: Path) -> None:
        """**`/results`（引数なし）が既定で見る日**（詳細設計 3.7）。

        時計を使わない — バッチが「照合した最も新しい試合日」を書く。
        """
        self._write(tmp_path, latest_result_date="2026-10-07")
        meta = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))["data"]
        assert meta["latestResultDate"] == "2026-10-07"

    def test_latest_result_date_is_carried_forward(self, tmp_path: Path) -> None:
        """**照合が無い回は前回の値を引き継ぐ**（`lastSuccessAt` と同じ型）。

        None を書くと `/results` が「まだ記録がありません」に戻る — 実際には
        記録があるのに空状態を出すことになる。
        """
        self._write(tmp_path, latest_result_date="2026-10-07")
        self._write(tmp_path, latest_result_date=None)
        meta = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))["data"]
        assert meta["latestResultDate"] == "2026-10-07"

    def test_latest_result_date_is_null_on_the_first_run(self, tmp_path: Path) -> None:
        """1試合も照合していない間は null。画面は空状態を出す（要件 8.5）。"""
        self._write(tmp_path)
        meta = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))["data"]
        assert meta["latestResultDate"] is None
        assert carry_forward_latest_result(tmp_path / "none", None) is None

    def test_broken_previous_meta_is_treated_as_missing(self, tmp_path: Path) -> None:
        """壊れた `meta.json` で落ちない。例外の本文もログに出さない（絶対ルール4）。"""
        tmp_path.mkdir(exist_ok=True)
        (tmp_path / "meta.json").write_text("{ not json", encoding="utf-8")
        assert carry_forward_last_success(tmp_path, GENERATED_AT, "PARTIAL") is None

    # ── 予測を出した試合の索引（詳細設計 5.6） ────────────────────

    def test_the_index_accumulates_across_runs(self, tmp_path: Path) -> None:
        """**索引は溜まる。** 窓から出た試合のIDが消えないこと。

        これが無いと、昨日の試合を押したときに 404 になる（2026-10-08 に
        運営者が実際に踏んだ）。過去の試合こそ予想と結果の対比の置き場である。
        """
        self._write(
            tmp_path,
            today=GameListInput(
                game_date="2026-10-07", games=[GameListRow(a_game(game_id="old"), a_prediction())]
            ),
            details=[GameDetailInput(game=a_game(game_id="old"), prediction=a_prediction())],
        )
        assert read_game_index(tmp_path) == ["old"]

        result = self._write(tmp_path)  # 翌日。窓は g1 だけになる
        assert read_game_index(tmp_path) == ["g1", "old"]
        assert result.indexed == 2
        # 詳細ファイルのほうは窓から出たので消える
        assert not (tmp_path / "games" / "old.json").exists()

    def test_the_index_survives_the_prune(self, tmp_path: Path) -> None:
        """削除が索引そのものを消さないこと。

        消すと次のビルドで過去の試合のページが1枚も作られなくなる。
        **`games/` の中にあるため、素朴な `*.json` の走査に当たる。**
        """
        self._write(tmp_path)
        assert (tmp_path / "games" / GAME_INDEX).exists()
        self._write(tmp_path, details=[])
        assert (tmp_path / "games" / GAME_INDEX).exists()
        assert read_game_index(tmp_path) == ["g1"]

    def test_only_predicted_games_enter_the_index(self, tmp_path: Path) -> None:
        """**予測の無い試合を索引に入れない。**

        入れても「この試合の予測はまだありません」と出るだけで、対比するものが
        無い（要件 8.5）。`GameBoardList` がリンクを張る条件と同じ定義である。
        """
        self._write(
            tmp_path,
            today=GameListInput(
                game_date="2026-09-26",
                games=[GameListRow(a_game(game_id="yes"), a_prediction()), GameListRow(a_game(game_id="no"), None)],
            ),
            details=[],
        )
        assert read_game_index(tmp_path) == ["yes"]

    def test_upcoming_games_enter_the_index(self, tmp_path: Path) -> None:
        """**窓の7日ぶんも索引に入れる。** 詳細ファイルは当日だけだが（3.7）、
        ページは `/api/v1/games/:id` から取れる（詳細設計 5.6）。"""
        self._write(
            tmp_path,
            upcoming=[
                GameListInput(
                    game_date="2026-09-27",
                    games=[GameListRow(a_game(game_id="tomorrow"), a_prediction())],
                )
            ],
        )
        assert read_game_index(tmp_path) == ["g1", "tomorrow"]

    def test_the_index_order_is_stable(self, tmp_path: Path) -> None:
        """**並びが実行ごとに動かないこと。** 動くと差分が無意味に膨らむ
        （リポジトリにコミットするファイルである）。"""
        self._write(
            tmp_path,
            today=GameListInput(
                game_date="2026-09-26",
                games=[GameListRow(a_game(game_id="b"), a_prediction()), GameListRow(a_game(game_id="a"), a_prediction())],
            ),
            details=[],
        )
        first = (tmp_path / "games" / GAME_INDEX).read_text(encoding="utf-8")
        self._write(
            tmp_path,
            today=GameListInput(
                game_date="2026-09-26",
                games=[GameListRow(a_game(game_id="a"), a_prediction()), GameListRow(a_game(game_id="b"), a_prediction())],
            ),
            details=[],
        )
        assert (tmp_path / "games" / GAME_INDEX).read_text(encoding="utf-8") == first

    def test_a_broken_index_is_treated_as_empty(self, tmp_path: Path) -> None:
        """**壊れていても書き出しを落とさない。** 初回と同じ扱いになり、
        その回の試合から溜め直す（`meta.json` と同じ方針）。"""
        (tmp_path / "games").mkdir()
        (tmp_path / "games" / GAME_INDEX).write_text("壊れた", encoding="utf-8")
        self._write(tmp_path)
        assert read_game_index(tmp_path) == ["g1"]

    def test_the_index_is_empty_before_the_first_run(self, tmp_path: Path) -> None:
        assert read_game_index(tmp_path) == []

    def test_window_longer_than_seven_days_is_rejected(self, tmp_path: Path) -> None:
        """詳細を7日窓にしないのと同じ理由で、一覧の窓も広げない（詳細設計 3.7）。"""
        days = [
            GameListInput(game_date=f"2026-10-{day:02d}")
            for day in range(1, SCHEDULE_WINDOW_DAYS + 2)
        ]
        with pytest.raises(ValueError):
            self._write(tmp_path, upcoming=days)

    def test_file_count_stays_within_the_ci_limit(self, tmp_path: Path) -> None:
        """`data/` のファイル数が上限以下であること（詳細設計 3.7）。"""
        self._write(
            tmp_path,
            upcoming=[
                GameListInput(game_date=f"2026-09-{day}") for day in range(27, 30)
            ],
            details=[
                GameDetailInput(game=a_game(game_id=f"g{i}"), prediction=a_prediction())
                for i in range(13)
            ],
        )
        assert count_data_files(tmp_path) <= MAX_DATA_FILES

    def test_no_files_are_written_when_the_writer_is_not_called(self, tmp_path: Path) -> None:
        """推論に失敗したら1ファイルも書かない（基本設計 4.3）。

        呼び出し側がこの関数を呼ばないことで実現する。**部分的に書く経路を持たない**
        ことを、書き出し前のディレクトリが空であることで固定する。
        """
        assert count_data_files(tmp_path) == 0
