"""結果照合と的中率の集計（詳細設計 4.12。`batch/jobs/evaluate.py`）。

受け入れ基準 A-04（**中止・延期は的中率の母数から除外される**）に対応する。

| テスト | A-04 / 4.12 のどの規約か |
|---|---|
| `test_cancelled_game_excluded_from_accuracy` | `VOID` は母数に入らない |
| `test_void_leaves_the_columns_null` | 0 を入れない（0 は「ホームが負けた」の意味を持つ） |
| `test_threshold_matches_metrics_accuracy` | 閾値を2箇所で別に決めない |
| `test_bucket_never_reaches_ten` | `p = 1.0` を10番目にしない |
| `test_bucket_rows_carry_the_predicted_mean` | `BUCKET` 行の `accuracy` 列は予想確率の平均 |
| `test_model_scope_keeps_the_version` | 横断の4スコープだけが空文字 |
| `test_stale_snapshot_is_skipped_not_guessed` | 状態を推測しない |
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from batch.jobs.evaluate import (
    ACROSS_MODELS,
    BUCKETS,
    EvaluateError,
    _result_payload,
    _summary_payload,
    bucket_label,
    bucket_of,
    evaluate,
    evaluate_one,
    summarize,
)
from batch.model.metrics import accuracy as metrics_accuracy
from batch.tests.test_static_json import contract, key_paths


def prediction(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "predictionId": "pred-1", "gameId": "g1", "seasonId": "s1",
        "modelVersion": "winner-v1.0.0", "homeWinProb": 0.68,
        "predHomeScore": 84.0, "predAwayScore": 78.0, "wasProvisional": 0,
    }
    base.update(over)
    return base


def game(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "g1", "status": "FINISHED", "home_score": 88.0, "away_score": 81.0,
        # `game_date` は DDL で NOT NULL（1.3）。`latestResultDate` の出どころ（3.7）
        "game_date": "2026-10-07",
    }
    base.update(over)
    return base


def games(rows: list[dict[str, object]]) -> pd.DataFrame:
    return pd.DataFrame(
        rows,
        columns=["id", "status", "home_score", "away_score", "game_date"])


# --- A-04: 中止・延期 ---

@pytest.mark.parametrize("status", ["CANCELLED", "POSTPONED"])
def test_cancelled_game_excluded_from_accuracy(status: str) -> None:
    """**`VOID` は的中率の母数から除外される**（A-04）。"""
    result = evaluate_one(
        prediction(), game(status=status, home_score=None, away_score=None))
    assert result.outcome == "VOID"
    assert not result.counted
    assert summarize([result]) == [], "VOID だけなら集計行を作らない"


def test_void_leaves_the_columns_null() -> None:
    """**0 を入れない。** 0 は「ホームが負けた」という意味を持ってしまう（4.12）。"""
    result = evaluate_one(prediction(), game(status="CANCELLED"))
    assert result.actual_home_win is None
    assert result.is_correct is None
    assert result.brier is None
    assert result.score_mae is None


def test_finished_without_scores_is_void() -> None:
    """**スコアが無ければ `FINISHED` でも `VOID`。** 0 として扱わない。"""
    result = evaluate_one(
        prediction(), game(home_score=None, away_score=None))
    assert result.outcome == "VOID"


def test_void_counts_are_reported() -> None:
    """母数と `VOID` を分けて数えられること。"""
    rows = [
        evaluate_one(prediction(predictionId="a"), game()),
        evaluate_one(prediction(predictionId="b", gameId="g2"),
                     game(id="g2", status="POSTPONED")),
    ]
    assert [r.counted for r in rows] == [True, False]


# --- 勝敗の判定 ---

def test_home_win_is_recorded() -> None:
    result = evaluate_one(prediction(), game(home_score=88.0, away_score=81.0))
    assert (result.outcome, result.actual_home_win) == ("WIN", 1)
    assert (result.predicted_home_win, result.is_correct) == (1, 1)


def test_away_win_is_recorded() -> None:
    result = evaluate_one(prediction(), game(home_score=81.0, away_score=88.0))
    assert (result.outcome, result.actual_home_win) == ("LOSS", 0)
    assert (result.predicted_home_win, result.is_correct) == (1, 0)


def test_threshold_matches_metrics_accuracy() -> None:
    """**閾値を2箇所で別に決めない**（4.12）。

    `metrics.accuracy()` は `(p > 0.5) == (y > 0.5)` で測る。食い違うと
    「的中率ページの数字が採用判定の数字と合わない」ことになる。
    """
    for prob in (0.0, 0.3, 0.5, 0.500001, 0.7, 1.0):
        result = evaluate_one(
            prediction(homeWinProb=prob), game(home_score=88.0, away_score=81.0))
        expected = metrics_accuracy(np.array([prob]), np.array([1.0]))
        assert float(result.is_correct or 0) == expected, f"p={prob}"


def test_exactly_half_is_counted_as_away() -> None:
    """ちょうど 0.5 はアウェイ側。**`metrics.accuracy()` と同じ**（`p > 0.5`）。"""
    result = evaluate_one(
        prediction(homeWinProb=0.5), game(home_score=88.0, away_score=81.0))
    assert result.predicted_home_win == 0


def test_brier_is_the_squared_error() -> None:
    result = evaluate_one(prediction(homeWinProb=0.68), game())
    assert result.brier == pytest.approx((0.68 - 1.0) ** 2)


def test_score_mae_is_the_mean_of_both_teams() -> None:
    """**両チーム得点の絶対誤差の平均**（4.12）。得点差の MAE と混同しない。"""
    result = evaluate_one(
        prediction(predHomeScore=84.0, predAwayScore=78.0),
        game(home_score=88.0, away_score=81.0))
    assert result.score_mae == pytest.approx((4.0 + 3.0) / 2.0)


def test_score_mae_is_none_without_a_predicted_score() -> None:
    """予想スコアが無ければ None。**0 で埋めない。**"""
    result = evaluate_one(
        prediction(predHomeScore=None, predAwayScore=None), game())
    assert result.score_mae is None


def test_bad_probability_is_rejected() -> None:
    for bad in (None, "x", -0.1, 1.1):
        with pytest.raises(EvaluateError, match="0〜1"):
            evaluate_one(prediction(homeWinProb=bad), game())


# --- ビン ---

def test_bucket_never_reaches_ten() -> None:
    """**`p = 1.0` を10番目にしない**（`prob_bucket` は 0〜9）。"""
    assert bucket_of(1.0) == BUCKETS - 1 == 9
    assert bucket_of(0.0) == 0
    assert bucket_of(0.68) == 6
    assert bucket_of(0.7) == 7
    assert all(0 <= bucket_of(p / 1000) <= 9 for p in range(1001))


def test_bucket_label_matches_the_public_api() -> None:
    """**公開APIがこの文字列をそのまま画面に出す**（3.3 の `calibration`）。"""
    assert bucket_label(6) == "60-70%"
    assert bucket_label(9) == "90-100%"
    assert bucket_label(0) == "0-10%"


# --- accuracy_summary ---

def sample() -> list:
    """的中2件・外れ1件・VOID1件。シーズンとモデルも分ける。"""
    return [
        evaluate_one(prediction(predictionId="a", homeWinProb=0.8), game()),
        evaluate_one(prediction(predictionId="b", gameId="g2", homeWinProb=0.7),
                     game(id="g2")),
        evaluate_one(prediction(predictionId="c", gameId="g3", homeWinProb=0.9,
                                wasProvisional=1),
                     game(id="g3", home_score=70.0, away_score=90.0)),
        evaluate_one(prediction(predictionId="d", gameId="g4", seasonId="s2",
                                modelVersion="winner-v1.1.0"),
                     game(id="g4", status="CANCELLED")),
    ]


def test_overall_has_the_home_always_baseline() -> None:
    """**`baseline_accuracy` は `OVERALL` にだけ入れる**（4.12）。

    「ホームが必ず勝つ」と予想した場合の的中率で、実績の平均である。
    """
    rows = summarize(sample())
    overall = [r for r in rows if r.scope == "OVERALL"]
    assert len(overall) == 1
    assert overall[0].scope_key == "all"
    assert overall[0].model_version == ACROSS_MODELS
    assert overall[0].n == 3, "VOID を母数に入れない"
    assert overall[0].accuracy == pytest.approx(2 / 3)
    assert overall[0].baseline_accuracy == pytest.approx(2 / 3)
    for row in rows:
        if row.scope != "OVERALL":
            assert row.baseline_accuracy is None


def test_model_scope_keeps_the_version() -> None:
    """**横断の4スコープだけが空文字**（1.6 / 4.12）。`MODEL` は実際の版を入れる。"""
    rows = summarize(sample())
    for row in rows:
        if row.scope == "MODEL":
            assert row.model_version == row.scope_key != ACROSS_MODELS
        else:
            assert row.model_version == ACROSS_MODELS


def test_no_null_model_version() -> None:
    """**`model_version` に NULL を入れない**（1.6。主キーの NULL 重複を避ける）。"""
    for row in summarize(sample()):
        assert row.model_version is not None


def test_bucket_rows_carry_the_predicted_mean() -> None:
    """**`BUCKET` 行の `accuracy` 列には「予想した確率の平均」を入れる**（4.12）。

    公開APIが `predicted: r.accuracy` / `actual: r.actual_rate` と読む。
    **列名と中身が一致していない唯一の箇所である。**
    """
    rows = summarize(sample())
    buckets = {r.scope_key: r for r in rows if r.scope == "BUCKET"}
    assert set(buckets) == {"80-90%", "70-80%", "90-100%"}
    assert buckets["80-90%"].accuracy == pytest.approx(0.8)
    assert buckets["80-90%"].actual_rate == pytest.approx(1.0)
    assert buckets["90-100%"].actual_rate == pytest.approx(0.0), "外れた試合"
    for row in buckets.values():
        assert row.brier is not None


def test_provisional_split() -> None:
    rows = {r.scope_key: r for r in summarize(sample()) if r.scope == "PROVISIONAL"}
    assert set(rows) == {"provisional", "confirmed"}
    assert rows["provisional"].n == 1
    assert rows["confirmed"].n == 2


def test_season_scope() -> None:
    rows = [r for r in summarize(sample()) if r.scope == "SEASON"]
    assert [r.scope_key for r in rows] == ["s1"], "VOID だけの s2 は行を作らない"


def test_empty_scopes_are_not_written() -> None:
    """**母数が0の行を作らない**（`accuracy` / `brier` は NOT NULL。4.12）。"""
    void_only = [evaluate_one(prediction(), game(status="CANCELLED"))]
    assert summarize(void_only) == []


def test_all_scopes_are_present() -> None:
    scopes = {r.scope for r in summarize(sample())}
    assert scopes == {"OVERALL", "SEASON", "MODEL", "BUCKET", "PROVISIONAL"}


# --- スナップショットとの突き合わせ ---

def test_stale_snapshot_is_skipped_not_guessed() -> None:
    """**状態を推測しない**（4.12）。

    D1 では終了していてもスナップショットが古いことがある。飛ばして報告する。
    """
    outcome = evaluate([prediction()], games([game(status="SCHEDULED")]))
    assert outcome.results == []
    assert outcome.skipped == [("g1", "スナップショットが SCHEDULED のまま")]


def test_missing_game_is_skipped() -> None:
    outcome = evaluate([prediction()], games([game(id="other")]))
    assert outcome.results == []
    assert [g for g, _ in outcome.skipped] == ["g1"]


def test_missing_columns_are_rejected() -> None:
    with pytest.raises(EvaluateError, match="必要な列がない"):
        evaluate([prediction()], pd.DataFrame({"id": ["g1"]}))


def test_evaluate_is_idempotent_in_shape() -> None:
    """同じ入力から同じ結果になること（照合は冪等。6.3）。"""
    frame = games([game()])
    first = evaluate([prediction()], frame)
    second = evaluate([prediction()], frame)
    assert first.results == second.results


# --- 内部APIへ送る形（契約ファイル） ---
#
# **「API と同じ形で送る」は言葉で決めても守られない。** 送る側は Python、
# 受ける側（Zod）は TypeScript で、同じコードを共有できない。キー構造だけを
# `contracts/public-shapes.json` に固定し、両方のテストがそれを読む
# （静的JSON で使っている仕組みと同じ。詳細設計 3.7 / 4.12）。


def test_evaluate_payload_matches_the_contract() -> None:
    """`POST /internal/evaluate` の本文のキー構造が契約と一致すること。"""
    rows = [evaluate_one(prediction(), game())]
    assert key_paths(_result_payload(rows)) == contract("internalEvaluate")


def test_summary_payload_matches_the_contract() -> None:
    """`POST /internal/summary` の本文のキー構造が契約と一致すること。"""
    rows = summarize(sample())
    assert key_paths(_summary_payload(rows)) == contract("internalSummary")


def test_void_rows_keep_every_key() -> None:
    """**`VOID` の行でもキーを消さない。** 値が `null` になるだけである。

    キーごと消すと、受け取る側が「ある場合とない場合」の2通りを持つことになる
    （3.3 の `prediction: null` と同じ考え方）。
    """
    void = [evaluate_one(prediction(), game(status="CANCELLED"))]
    assert key_paths(_result_payload(void)) == contract("internalEvaluate")


# --- 予想スコアの誤差（基本設計 5.2 / 詳細設計 4.12） ---

def test_score_mae_is_per_team() -> None:
    """**1チームあたりの平均絶対誤差。** 得点差の MAE とは別物である（要件 6.4）。

    予想 84–78 / 実際 88–81 なら、ホーム4点・アウェイ3点 → 平均 3.5点。
    得点差は 6 対 7 で誤差1点であり、**値が違う**。
    """
    result = evaluate_one(prediction(), game())
    assert result.score_mae == pytest.approx(3.5)


def test_score_mae_is_averaged_over_the_scope() -> None:
    rows = summarize(sample())
    overall = next(r for r in rows if r.scope == "OVERALL")
    counted = [r for r in sample() if r.counted]
    expected = sum(r.score_mae or 0.0 for r in counted) / len(counted)
    assert overall.score_mae == pytest.approx(expected)


def test_score_mae_is_null_when_any_row_lacks_it() -> None:
    """**1行でも欠けていれば NULL**（4.12）。

    欠けた行を除いて平均すると、画面の「N試合中…」の隣に**母数の違う数字が
    並ぶ**。要件 8.3 の「母数を併記する」は、併記した母数がその数字のもので
    あることを前提にしている。
    """
    rows = summarize([
        evaluate_one(prediction(), game()),
        evaluate_one(
            prediction(predictionId="b", gameId="g2", predHomeScore=None,
                       predAwayScore=None),
            game(id="g2")),
    ])
    overall = next(r for r in rows if r.scope == "OVERALL")
    assert overall.n == 2, "勝敗の母数は2件のまま"
    assert overall.score_mae is None, "除いて平均しない"


def test_score_mae_is_not_zero_when_missing() -> None:
    """**0 を入れない。** 0 は「誤差なし」という意味を持つ。"""
    rows = summarize([
        evaluate_one(
            prediction(predHomeScore=None, predAwayScore=None), game()),
    ])
    assert next(r for r in rows if r.scope == "OVERALL").score_mae is None


def test_every_scope_carries_the_score_mae() -> None:
    """**集計の規則をスコープで分けない**（4.12）。画面に出すのは3つだけだが。"""
    rows = summarize(sample())
    assert {r.scope for r in rows} >= {"OVERALL", "SEASON", "MODEL", "BUCKET"}
    for row in rows:
        assert row.score_mae is not None, row.scope


# --- `latestResultDate`（詳細設計 3.7） ---

def test_latest_result_date_is_the_newest_counted_game() -> None:
    """**照合した、母数に入る試合の最も新しい日**を返す。

    `/results`（引数なし）が既定で見る日になる。**並び順に依らない** —
    `pending` が返す順序は決まっていない。
    """
    out = evaluate(
        [prediction(predictionId="p1", gameId="g1"),
         prediction(predictionId="p2", gameId="g2"),
         prediction(predictionId="p3", gameId="g3")],
        games([
            game(id="g1", game_date="2026-10-07"),
            game(id="g3", game_date="2026-10-05"),
            game(id="g2", game_date="2026-10-09"),
        ]),
    )
    assert len(out.results) == 3
    assert out.latest_result_date == "2026-10-09"


def test_void_games_do_not_set_the_latest_result_date() -> None:
    """**`VOID` は数えない。** 中止・延期は `/results` に出ない（3.3）。

    数えてしまうと、画面が「その日の結果」を見に行って空を出す。
    """
    out = evaluate(
        [prediction(predictionId="p1", gameId="g1"),
         prediction(predictionId="p2", gameId="g2")],
        games([
            game(id="g1", game_date="2026-10-07"),
            game(id="g2", game_date="2026-10-09", status="CANCELLED",
                 home_score=None, away_score=None),
        ]),
    )
    assert [r.outcome for r in out.results].count("VOID") == 1
    assert out.latest_result_date == "2026-10-07"


def test_no_match_leaves_the_latest_result_date_unset() -> None:
    """照合が0件なら None。書き出し側が前回の値を引き継ぐ（3.7）。"""
    assert evaluate([], games([])).latest_result_date is None
