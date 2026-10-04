"""根拠のグループ集約（詳細設計 2.7 / 2.7.1）。"""

from __future__ import annotations

from collections import Counter

import numpy as np
import pytest

from batch.features.builder import DEFAULTS, FEATURE_KEYS
from batch.model.baselines import Logistic
from batch.model.explain import (
    GROUP_ORDER,
    WORDING,
    Explainer,
    ExplainError,
    logit,
    payload_of,
)


def explainer(
    coefficients: dict[str, float] | None = None,
    *,
    intercept: float = 0.0,
    means: dict[str, float] | None = None,
) -> Explainer:
    """挙げた列だけ係数・平均を持つ Explainer（他は 0）。"""
    coefficients = coefficients or {}
    means = means or {}
    return Explainer(
        features=tuple(FEATURE_KEYS),
        intercept=intercept,
        coefficients=np.array([coefficients.get(k, 0.0) for k in FEATURE_KEYS]),
        means=np.array([means.get(k, 0.0) for k in FEATURE_KEYS]),
    )


def features(**over: float) -> dict[str, float]:
    return {**DEFAULTS, **over}


# --- 文言表（2.7.1） ---


def test_the_wording_table_covers_every_feature() -> None:
    """**列を足して文言を忘れたまま動かさない。**

    忘れると `WORDING[k]` が実行時に KeyError になり、**本番の推論で初めて落ちる**。
    取り込み時にも照合しているが、ここでも明示する。
    """
    assert set(WORDING) == set(FEATURE_KEYS)


def test_every_group_is_known() -> None:
    assert {w.group for w in WORDING.values()} <= set(GROUP_ORDER)


def test_venue_has_no_column_and_player_has_three() -> None:
    """**現在の21列では `VENUE` に1本もない**（2.7.1）。

    #15 を落とし、#16 と #29 を実装ごと削除したためである。**この表が崩れたら
    「出る根拠は2件」という文書の記述も崩れる**ため、ここで固定する。
    """
    counts = Counter(w.group for w in WORDING.values())
    assert counts["TEAM_STRENGTH"] == 13
    assert counts["SCHEDULE"] == 5
    assert counts["PLAYER"] == 3
    assert counts["VENUE"] == 0


#: 水準の列（差ではない）。**値そのものを出すため絶対値を取らない。**
#: いずれも構造上非負である（Elo・戦目・連続アウェイ試合数）。
LEVEL_COLUMNS = ("elo_home", "elo_away", "series_game_no", "away_streak_away")


def test_the_difference_columns_carry_no_sign() -> None:
    """**符号を出さない**（どちらのチームから見た符号なのかが画面に出ていない）。

    係数が負の列では差の向きと有利な側が逆になる（`drtg_diff`）。差の列は
    大きさだけを出し、向きは `favors` が持つ。
    """
    for key, wording in WORDING.items():
        if key in LEVEL_COLUMNS:
            continue
        assert wording.render(-12.0) == wording.render(12.0), key


def test_the_level_columns_are_the_only_exception() -> None:
    """**例外を名前で固定する。** 増えたらここで落ちる。"""
    exceptional = [
        k for k, w in WORDING.items() if w.render(-12.0) != w.render(12.0)
    ]
    assert sorted(exceptional) == sorted(LEVEL_COLUMNS)


def test_no_label_names_elo() -> None:
    """**ラベルに専門用語を出さない**（2.7 の表示例も「チーム力の差」と書く）。"""
    for key, wording in WORDING.items():
        assert "Elo" not in wording.label, key
        assert "elo" not in wording.label, key


def test_percent_is_reserved_for_win_probability() -> None:
    """**`%` は勝率専用**（要件 8.3）。率の差は「ポイント」で書く。"""
    for key, wording in WORDING.items():
        assert "%" not in wording.render(0.123), key


# --- 恒等式（2.7） ---


def test_contributions_and_base_value_reconstruct_the_log_odds() -> None:
    """**近似ではなく恒等式である。**

    `切片 + Σ(係数 × 平均) + Σ(係数 × (特徴量 − 平均)) = logit(p)`。
    """
    # **確率が飽和しない大きさにする。** `Logistic.predict` は [eps, 1-eps] に
    # クリップするため、ログオッズが30を超えると恒等式を確かめられない
    coefficients = {k: 0.0005 * (i + 1) for i, k in enumerate(FEATURE_KEYS)}
    means = {k: float(i) for i, k in enumerate(FEATURE_KEYS)}
    exp = explainer(coefficients, intercept=0.12, means=means)
    model = Logistic(
        intercept=0.12,
        coefficients=np.array([coefficients[k] for k in FEATURE_KEYS]),
    )
    values = features(elo_diff=82.0, rest_days_diff=2.0, margin_l5_diff=6.2)
    row = np.array([[values[k] for k in FEATURE_KEYS]])
    probability = float(model.predict(row)[0])

    total = exp.base_value + sum(exp.contributions(values).values())
    assert total == pytest.approx(logit(probability), abs=1e-9)


def test_contributions_need_every_column() -> None:
    exp = explainer({"elo_diff": 0.01})
    with pytest.raises(ExplainError):
        exp.contributions({"elo_diff": 1.0})


def test_a_non_finite_feature_is_refused() -> None:
    exp = explainer({"elo_diff": 0.01})
    with pytest.raises(ExplainError):
        exp.contributions(features(elo_diff=float("nan")))


def test_the_explainer_refuses_a_mismatched_size() -> None:
    with pytest.raises(ExplainError):
        Explainer(features=("a", "b"), intercept=0.0,
                  coefficients=np.array([1.0]), means=np.array([0.0, 0.0]))


# --- グループ集約 ---


def test_a_group_whose_total_is_zero_has_no_row() -> None:
    """**向きが定まらない行を作らない**（2.7.1）。

    `PLAYER` の3列は `game_entries` が0行のため定数で、`特徴量 − 平均` が
    厳密に 0 になる。「選手 → ホーム有利」は事実に反する。
    """
    exp = explainer({"elo_diff": 0.01, "minutes_lost_diff": 0.5})
    rows = exp.reasons(features(elo_diff=82.0))
    assert [r.group_key for r in rows] == ["TEAM_STRENGTH"]


def test_the_production_shape_yields_two_reasons() -> None:
    """**本番で出る根拠は2件である**（2.7.1）。

    `VENUE` に列がなく、`PLAYER` の3列は定数で寄与が 0 になる。**したがって
    受け入れ基準 A-01（3件以上）は工程13 だけでは満たさない。**
    """
    coefficients = {k: 1.0 for k in FEATURE_KEYS}
    rows = explainer(coefficients).reasons(features())
    assert [r.group_key for r in rows] == ["TEAM_STRENGTH", "SCHEDULE"]
    assert len(rows) < 3


def test_rows_are_ranked_by_the_size_of_the_contribution() -> None:
    exp = explainer({"elo_diff": 1.0, "rest_days_diff": 10.0})
    rows = exp.reasons(features(elo_diff=3.0, rest_days_diff=2.0))
    assert [r.group_key for r in rows] == ["SCHEDULE", "TEAM_STRENGTH"]
    assert [r.rank for r in rows] == [1, 2]


def test_favors_follows_the_sign_of_the_group_total() -> None:
    exp = explainer({"elo_diff": 1.0})
    assert exp.reasons(features(elo_diff=5.0))[0].favors == "HOME"
    assert exp.reasons(features(elo_diff=-5.0))[0].favors == "AWAY"


def test_base_value_is_the_same_on_every_row() -> None:
    """1予測の全グループで同じ値である（2.7.1）。"""
    exp = explainer({"elo_diff": 1.0, "rest_days_diff": 1.0}, intercept=0.3,
                    means={"elo_diff": 4.0})
    rows = exp.reasons(features(elo_diff=5.0, rest_days_diff=2.0))
    assert len({r.base_value for r in rows}) == 1
    assert rows[0].base_value == pytest.approx(0.3 + 1.0 * 4.0)


# --- 代表項目（2.7.1） ---


def test_the_representative_matches_the_direction_of_the_group() -> None:
    """**向きを問わずに最大を採らない。**

    採ると、グループが「ホーム有利」なのに代表の事実が逆を向く行が出る。
    ここでは `elo_diff` が最大（−10）だが、合計は +5 でホーム有利である。
    """
    exp = explainer({k: 1.0 for k in FEATURE_KEYS})
    rows = exp.reasons(features(
        elo_diff=-10.0, winrate_l5_diff=6.0, winrate_l10_diff=6.0,
        winrate_season_diff=3.0, elo_home=0.0, elo_away=0.0,
        series_game_no=0.0,
    ))
    assert len(rows) == 1
    assert rows[0].favors == "HOME"
    assert rows[0].label_ja == "直近5試合の勝率の差"
    assert rows[0].label_ja != "チーム力の差"


def test_entry_is_official_is_never_the_representative() -> None:
    """**データの状態を表す列は代表に選ばない**（2.7.1）。

    「出場選手の発表 / 未発表 / ホーム有利」は事実として成立しない。寄与は
    グループ合計に入れるが、文言には出さない。
    """
    exp = explainer({"entry_is_official": 2.0, "minutes_lost_diff": 0.1})
    rows = exp.reasons(features(entry_is_official=1.0, minutes_lost_diff=1.0))
    assert [r.group_key for r in rows] == ["PLAYER"]
    assert rows[0].label_ja == "欠場者の出場時間の差"


def test_the_representative_is_deterministic_on_a_tie() -> None:
    """同じ寄与なら `FEATURE_KEYS` の順で先の列を採る（実行ごとに変わらない）。"""
    exp = explainer({"winrate_l5_diff": 1.0, "winrate_l10_diff": 1.0})
    rows = exp.reasons(features(winrate_l5_diff=4.0, winrate_l10_diff=4.0))
    assert rows[0].label_ja == "直近5試合の勝率の差"


# --- 書式 ---


def test_the_rendered_values_read_as_facts() -> None:
    exp = explainer({k: 1.0 for k in FEATURE_KEYS})
    rows = {r.group_key: r for r in exp.reasons(features(
        elo_diff=-82.4, elo_home=0.0, elo_away=0.0, rest_days_diff=2.0,
        series_game_no=0.0,
    ))}
    assert rows["TEAM_STRENGTH"].value_text == "82ポイント"
    assert rows["SCHEDULE"].value_text == "2日"


def test_a_level_column_keeps_its_own_value() -> None:
    """水準の列（差ではない）は絶対値を取らない。"""
    assert WORDING["series_game_no"].render(2.0) == "2戦目"
    assert WORDING["elo_home"].render(1582.4) == "1582ポイント"


def test_a_choice_column_reads_as_a_sentence() -> None:
    assert WORDING["prev_result_diff"].render(1.0) == "勝敗が分かれた"
    assert WORDING["prev_result_diff"].render(0.0) == "勝敗が同じ"
    assert WORDING["entry_is_official"].render(1.0) == "発表済み"
    assert WORDING["entry_is_official"].render(0.0) == "未発表"


def test_the_payload_matches_the_zod_schema_keys() -> None:
    """`api/src/schemas/predictions.ts` の `reasonSchema` と1対1で対応させる。"""
    exp = explainer({"elo_diff": 1.0})
    body = payload_of(exp.reasons(features(elo_diff=5.0))[0])
    assert set(body) == {
        "rank", "groupKey", "labelJa", "valueText", "favors",
        "contribution", "baseValue",
    }
    assert 1 <= len(str(body["labelJa"])) <= 128
    assert 1 <= len(str(body["valueText"])) <= 128
