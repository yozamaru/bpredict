"""未実施の試合の取り込み（詳細設計 4.2 のステップ1b / 基本設計 4.2 の3b）。

| テスト | どの規約か |
|---|---|
| `test_scores_are_null` | NULL = 未実施（詳細設計 1.3）。0 を入れると引き分けの意味になる |
| `test_no_venue_is_sent` | **日程ページに公式の会場IDが無い**。NULL のままにする |
| `test_no_club_seasons_are_sent` | **クラブ名は略称のことがある**。正式名称を上書きしない |
| `test_two_team_rows_per_game` | チーム視点の行は1試合2本 |
| `test_payload_matches_the_zod_schema` | 本文のキーが api 側の Zod と対応する |

**推論の本文（ステップ4）も同じファイルで検証する。** どちらも
`POST /internal/*` に送る本文であり、契約ファイルで固定する対象が同じである。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from batch.loader.payload import (
    PayloadError,
    SeasonRef,
    player_prediction_payload,
    prediction_payload,
    team_target_payload,
    upcoming_games_payload,
)
from batch.model.predict import Prediction
from batch.parser.schedule_parser import ScheduleGame

SEASON = SeasonRef("2026-27-PREMIER", "2026-27", "PREMIER")
CLUBS = {"703": "703", "704": "704"}


def game(**over: object) -> ScheduleGame:
    base: dict[str, object] = {
        "game_id": "600001",
        "competition": "REGULAR",
        "game_date": "2026-10-10",
        "tipoff_at": "2026-10-10T10:05:00Z",
        "home_source_id": "703",
        "away_source_id": "704",
        # **略称が来ることがある**（2020-21 の `千葉J`）。だから送らない
        "home_name": "架空ブ",
        "away_name": "架空タ",
        "home_score": None,
        "away_score": None,
        "status": "SCHEDULED",
        "source_url": "https://example.invalid/schedule/",
    }
    base.update(over)
    return ScheduleGame(**base)  # type: ignore[arg-type]


def payload(*games: ScheduleGame, series: dict[str, int] | None = None) -> dict[str, object]:
    return upcoming_games_payload(
        list(games) or [game()],
        season=SEASON,
        club_ids=CLUBS,
        series_game_no=series or {},
        fetched_at="2026-10-04T00:00:00Z",
    )


def rows(body: dict[str, object], key: str) -> list[dict[str, object]]:
    value = body[key]
    assert isinstance(value, list)
    return [row for row in value if isinstance(row, dict)]


# --- 未実施であることの表し方 ---

def test_scores_are_null() -> None:
    """**NULL = 未実施**（詳細設計 1.3）。0 を入れると引き分けの意味になる。"""
    row = rows(payload(), "games")[0]
    assert row["homeScore"] is None
    assert row["awayScore"] is None
    assert row["attendance"] is None


def test_finished_at_is_null_and_not_estimated() -> None:
    """未実施は `finished_at` が NULL、推定フラグは 0（詳細設計 1.3 の表）。"""
    row = rows(payload(), "games")[0]
    assert row["finishedAt"] is None
    assert row["finishedAtIsEstimated"] == 0


def test_team_rows_have_no_result_or_margin() -> None:
    """`result` / `margin` も NULL。**0 を入れない。**"""
    for row in rows(payload(), "teamGames"):
        assert row["result"] is None
        assert row["margin"] is None
        assert row["finishedAt"] is None


def test_two_team_rows_per_game() -> None:
    """チーム視点の行は1試合2本（ホームとアウェイ）。"""
    body = payload()
    assert len(rows(body, "teamGames")) == 2 * len(rows(body, "games"))
    assert {r["isHome"] for r in rows(body, "teamGames")} == {0, 1}


def test_opponents_are_crossed() -> None:
    for row in rows(payload(), "teamGames"):
        assert row["clubId"] != row["opponentId"]


# --- 送らないもの ---

def test_no_venue_is_sent() -> None:
    """**日程ページに公式の会場IDが無い**（詳細設計 2.2）。

    会場名で名寄せしない（1.1）ため NULL のままにし、試合後にボックススコアの
    取り込みが埋める。**`venues` の行を作らない** — 作ると ID の無い会場が増える。
    """
    body = payload()
    assert rows(body, "games")[0]["venueId"] is None
    assert rows(body, "games")[0]["venueNameAtGame"] is None
    assert "venues" not in body
    assert "venueSourceKeys" not in body


def test_no_club_seasons_are_sent() -> None:
    """**クラブ名は略称のことがある**（詳細設計 4.4）。

    `club_seasons.name` の出典はボックススコアの `TeamNameJ`（その試合時点の
    正式名称）である。**ここで略称を入れると正式名称を上書きする。**
    """
    assert "clubSeasons" not in payload()


def test_no_players_are_sent() -> None:
    """試合前には出場者が分からない（エントリーは別の口）。"""
    assert "players" not in payload()


# --- 状態 ---

@pytest.mark.parametrize("status", ["SCHEDULED", "POSTPONED", "CANCELLED"])
def test_non_finished_statuses_are_carried(status: str) -> None:
    """**`POSTPONED` と `CANCELLED` も送る。**

    `SCHEDULED` だけを入れると、**中止になった試合が `SCHEDULED` のまま残り、
    予測が作られ続ける**（詳細設計 1.3 の「ゴースト試合」）。
    """
    assert rows(payload(game(status=status)), "games")[0]["status"] == status


def test_series_game_no_is_passed_through() -> None:
    body = payload(game(), series={"600001": 2})
    assert rows(body, "games")[0]["seriesGameNo"] == 2


def test_series_game_no_is_none_when_unknown() -> None:
    """**推測で埋めない。** 連戦番号が決まらなければ NULL。"""
    assert rows(payload(), "games")[0]["seriesGameNo"] is None


# --- api 側との対応 ---

def test_payload_matches_the_zod_schema() -> None:
    """本文のキーが `api/src/schemas/facts.ts` に存在すること。

    片方だけ直すと本番で 400 を受けて初めて分かる（詳細設計 3.7 と同じ問題）。
    """
    schema = (Path(__file__).resolve().parents[2]
              / "api" / "src" / "schemas" / "facts.ts").read_text(encoding="utf-8")
    body = payload()
    assert set(body) <= set(re.findall(r"^\s{4}(\w+):", schema, re.MULTILINE)) | {
        "games", "teamGames"}
    for key in rows(body, "games")[0]:
        assert f"{key}:" in schema, f"games に Zod に無いキーがある: {key}"
    for key in rows(body, "teamGames")[0]:
        assert f"{key}:" in schema, f"teamGames に Zod に無いキーがある: {key}"


def test_body_is_json_serialisable() -> None:
    json.dumps(payload(), ensure_ascii=False)


# --- 予測の本文（契約。詳細設計 3.7 / 4.2） ---

CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "public-shapes.json"


def key_paths(value: object, prefix: str = "") -> set[str]:
    if isinstance(value, dict):
        out: set[str] = set()
        for key, child in value.items():
            out |= key_paths(child, f"{prefix}.{key}" if prefix else key)
        return out
    if isinstance(value, list):
        if not value:
            return {f"{prefix}[]"}
        out = set()
        for child in value:
            out |= key_paths(child, f"{prefix}[]")
        return out
    return {prefix}


#: 整合化を通したチーム目標（14項目）。**得点は持たない**（恒等式で導出する）
A_TARGET: dict[str, float] = {
    "fg2a": 43.0, "fg3a": 25.0, "fta": 18.0,
    "fg2_pct": 0.52, "fg3_pct": 0.34, "ft_pct": 0.78,
    "oreb": 9.0, "dreb": 26.0, "ast": 19.0, "tov": 12.0,
    "stl": 6.0, "blk": 2.0, "pf": 17.0, "fd": 17.0,
}


def a_team_target(*, is_home: bool) -> dict[str, object]:
    return team_target_payload(
        club_id="703" if is_home else "704", is_home=is_home, targets=A_TARGET)


def a_player() -> dict[str, object]:
    return player_prediction_payload(
        player_id="8582", club_id="703", avail_prob=0.95, minutes=31.2,
        counts={k: v for k, v in A_TARGET.items() if not k.endswith("_pct")},
        pcts={k: v for k, v in A_TARGET.items() if k.endswith("_pct")},
    )


def a_reason() -> dict[str, object]:
    return {
        "rank": 1, "groupKey": "TEAM_STRENGTH", "labelJa": "チーム力の差",
        "valueText": "82ポイント", "favors": "HOME",
        "contribution": 0.41, "baseValue": 0.12,
    }


def a_prediction(*, children: bool = True) -> dict[str, object]:
    return prediction_payload(
        game_id="g1", season_id="2026-27-PREMIER", run_id="daily-abc",
        predicted_at="2026-10-05T21:00:00Z", as_of="2026-10-06T10:05:00Z",
        data_as_of="2026-10-04T12:00:00Z",
        prediction=Prediction(home_win_prob=0.68, margin=7.3, total=162.0,
                              home_score=84.65, away_score=77.35),
        features={"elo_diff": 80.0, "rest_days_diff": 1.0},
        model_versions={"WINNER": "winner-v1.0.0", "MARGIN": "margin-v1.0.0",
                        "TOTAL": "total-v1.0.0"},
        reasons=[a_reason()] if children else (),
        team_targets=(
            [a_team_target(is_home=True), a_team_target(is_home=False)]
            if children else ()
        ),
        player_predictions=[a_player()] if children else (),
        rate_versions=(
            {("PLAYER_AVAIL", ""): "player_avail-v1.0.0",
             ("TEAM_RATE", "fg2a"): "team_rate-fg2a-v1.0.0"}
            if children else None
        ),
    )


def test_prediction_payload_matches_the_contract() -> None:
    """**`api/src/schemas/predictions.ts` の Zod と1対1で対応させる。**

    api 側は契約から組んだ本文が 200 で通ることまで見る（キーの一致だけでは
    値域を満たしていない場合を捕まえられない）。
    """
    contract = set(json.loads(CONTRACT.read_text(encoding="utf-8"))
                   ["internalPredictions"]["paths"])
    assert key_paths(a_prediction()) == contract


def test_children_are_absent_when_they_cannot_be_made() -> None:
    """**出せないものはキーを送らない**（詳細設計 4.2 の推論）。

    30本が揃っていない回と、個人スタッツを破棄した試合がこれである。
    """
    body = a_prediction(children=False)
    for key in ("teamTargets", "playerPredictions", "reasons"):
        assert key not in body


def test_one_sided_team_targets_are_rejected() -> None:
    """**`teamTargets` は2件か0件**（Zod の `refine` と同じ条件。3.4）。

    1件は片側だけ整合化した状態であり、**原理的に誤りである** — 送る前に落とす。
    """
    with pytest.raises(PayloadError, match="2件か0件"):
        prediction_payload(
            game_id="g1", season_id="s1", run_id="r", predicted_at="t",
            as_of="t", data_as_of="t",
            prediction=Prediction(0.5, 0.0, 160.0, 80.0, 80.0),
            features={}, model_versions={"WINNER": "w"},
            team_targets=[a_team_target(is_home=True)])


def test_players_without_team_targets_are_rejected() -> None:
    """**チーム目標なしに個人スタッツを送れない。**

    整合化はチーム目標に合わせる計算であり（2.4）、目標が無い個人スタッツは
    「何に整合しているのか」が無い。
    """
    with pytest.raises(PayloadError, match="チーム目標"):
        prediction_payload(
            game_id="g1", season_id="s1", run_id="r", predicted_at="t",
            as_of="t", data_as_of="t",
            prediction=Prediction(0.5, 0.0, 160.0, 80.0, 80.0),
            features={}, model_versions={"WINNER": "w"},
            player_predictions=[a_player()])


def test_the_bundle_carries_the_target_for_the_thirty(self_check: None = None) -> None:
    """**30本は `target` を持つ**（詳細設計 4.5.1）。

    `prediction_model_bundle` の主キーは `(prediction_id, model_type, target)`
    であり、14本を `target` なしで入れると1本目以外が主キー違反で落ちる。
    """
    bundle = a_prediction()["modelBundle"]
    assert isinstance(bundle, list)
    rows = {(str(m["modelType"]), str(m["target"])) for m in bundle}
    assert ("TEAM_RATE", "fg2a") in rows
    assert ("PLAYER_AVAIL", "") in rows
    assert ("WINNER", "") in rows


def test_the_error_columns_are_not_sent() -> None:
    """**`err_*` を送らない**（要件 6.8.6 の `N` が未定義。2.3.1）。

    **0 を入れない** — 0 は「誤差がない」という意味を持ってしまう。
    """
    player = a_player()
    assert not [k for k in player if str(k).startswith("err")]


def test_the_representative_version_is_the_winner() -> None:
    """`predictions.model_version` は代表バージョン。全体は `modelBundle`（1.6）。"""
    body = a_prediction()
    assert body["modelVersion"] == "winner-v1.0.0"
    bundle = body["modelBundle"]
    assert isinstance(bundle, list)
    assert {str(m["modelType"]) for m in bundle} >= {"WINNER", "MARGIN", "TOTAL"}


def test_the_prediction_is_always_provisional() -> None:
    """エントリー情報を取得していないため常に暫定（要件 F-06 / 5.5）。"""
    assert a_prediction()["isProvisional"] == 1


def test_the_feature_snapshot_is_json() -> None:
    """`feature_snapshot` は JSON 文字列（1.5 の列のコメント）。"""
    snapshot = a_prediction()["featureSnapshot"]
    assert isinstance(snapshot, str)
    assert json.loads(snapshot)["elo_diff"] == pytest.approx(80.0)


def test_an_empty_model_bundle_is_rejected() -> None:
    """使ったモデルが分からない予測を作らない（出自を復元できなくなる。1.6）。"""
    with pytest.raises(PayloadError):
        prediction_payload(
            game_id="g1", season_id="s1", run_id="r", predicted_at="t",
            as_of="t", data_as_of="t",
            prediction=Prediction(0.5, 0.0, 160.0, 80.0, 80.0),
            features={}, model_versions={})
