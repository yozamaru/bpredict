"""採用基準（詳細設計 4.6）。純粋な関数なので合成データだけで検証する。"""
from __future__ import annotations

import pytest

from batch.model.criteria import (
    ECE_FLOOR_K,
    KNOWN_CONSTANT,
    MAX_FEATURE_NULL_RATE,
    MIN_EFFECT,
    MIN_EVAL_N,
    CriteriaError,
    Inputs,
    passes_criteria,
)
from batch.model.metrics import Difference


def inputs(**over: object) -> Inputs:
    """すべての条件を満たす入力。1つずつ崩して検証する。"""
    base: dict[str, object] = {
        "n": 1200,
        "brier": 0.2000,
        "baseline_elo_brier": 0.2150,
        "null_rates": {"elo_diff": 0.0},
        "constant_columns": [],
        "ece": 0.0300,
        "ece_floor": 0.0400,
        "current_brier": 0.2050,
        "difference": Difference(point=0.0050, ci_low=0.0010, ci_high=0.0090),
    }
    base.update(over)
    return Inputs(**base)  # type: ignore[arg-type]


def test_all_conditions_met_is_adopted() -> None:
    decision = passes_criteria(inputs())
    assert decision.adopt
    assert decision.failures == []


# --- n の下限 ---

def test_below_the_sample_floor_stops_before_counting_conditions() -> None:
    """**他の条件を見ない。** 「4つ満たして1つ落ちた」という読み方を誘わない。"""
    decision = passes_criteria(inputs(n=MIN_EVAL_N - 1, brier=0.9, baseline_elo_brier=0.1))
    assert not decision.adopt
    assert len(decision.failures) == 1
    assert "下限" in decision.failures[0]


def test_exactly_at_the_floor_is_evaluated() -> None:
    assert passes_criteria(inputs(n=MIN_EVAL_N)).adopt


# --- ベースライン ---

def test_not_beating_the_elo_baseline_fails() -> None:
    """超えられなければ LightGBM を使う理由がない（要件 6.4）。"""
    decision = passes_criteria(inputs(baseline_elo_brier=0.1990))
    assert not decision.adopt
    assert any("Elo単体" in f for f in decision.failures)


def test_tying_the_baseline_is_not_enough() -> None:
    assert not passes_criteria(inputs(baseline_elo_brier=0.2000)).adopt


# --- 現行モデルとの比較 ---

def test_worse_than_the_current_model_fails() -> None:
    decision = passes_criteria(inputs(brier=0.2100))
    assert not decision.adopt
    assert any("現行" in f for f in decision.failures)


def test_improvement_below_the_minimum_effect_fails() -> None:
    """有意でも、点推定の差が小さければ採用しない。"""
    small = MIN_EFFECT / 2
    decision = passes_criteria(inputs(
        current_brier=0.2000 + small,
        difference=Difference(point=small, ci_low=small / 2, ci_high=small * 2),
    ))
    assert not decision.adopt
    assert any("最小実質差" in f for f in decision.failures)


def test_insignificant_difference_fails() -> None:
    """信頼区間が0を跨ぐなら採用しない。"""
    decision = passes_criteria(inputs(
        difference=Difference(point=0.0050, ci_low=-0.0010, ci_high=0.0110),
    ))
    assert not decision.adopt
    assert any("信頼区間" in f for f in decision.failures)


def test_missing_bootstrap_with_a_current_model_is_an_error() -> None:
    """**黙って通さない。** 比較すべき相手がいるのに材料がないのは呼び出し側の誤り。"""
    with pytest.raises(CriteriaError):
        passes_criteria(inputs(difference=None))


# --- 初回登録（現行モデルがない） ---

def test_first_model_skips_the_comparison_but_keeps_everything_else() -> None:
    decision = passes_criteria(inputs(current_brier=None, difference=None))
    assert decision.adopt
    assert any("現行モデルがない" in n for n in decision.notes)


def test_first_model_still_has_to_beat_the_baseline() -> None:
    decision = passes_criteria(
        inputs(current_brier=None, difference=None, baseline_elo_brier=0.1990),
    )
    assert not decision.adopt


def test_a_blocked_comparison_is_not_treated_as_a_first_model() -> None:
    """**比較できなかったときは採用しない。**

    `current_brier=None` には2つの意味がある — 「現行モデルがない（初回登録）」と
    「現行モデルはあるが比較できなかった」。**後者を前者として扱うと、条件1〜2 を
    課さずに採用する** — 列を変えて `--initial` を忘れたときに起きる。
    """
    decision = passes_criteria(inputs(
        current_brier=None, difference=None,
        comparison_blocked="現行モデルの列が今の行列に無い（3列）",
    ))
    assert not decision.adopt
    assert any("比較できないため採用しない" in f for f in decision.failures)
    # **note ではなく failure である。** note は採用を止めない
    assert not any("現行モデルがない" in n for n in decision.notes)


def test_a_blocked_comparison_does_not_need_the_bootstrap() -> None:
    """比較していないのだから差の信頼区間は無い。**例外にしない。**"""
    decision = passes_criteria(inputs(
        current_brier=None, difference=None, comparison_blocked="artifact を読めない",
    ))
    assert not decision.adopt


# --- 較正 ---

def test_ece_above_the_noise_floor_threshold_fails() -> None:
    floor = 0.0400
    decision = passes_criteria(inputs(ece=floor * ECE_FLOOR_K, ece_floor=floor))
    assert not decision.adopt
    assert any("ECE" in f for f in decision.failures)


def test_ece_scales_with_the_floor_not_a_fixed_value() -> None:
    """**固定値 0.05 を使わない。** 同じ ECE でもフロア次第で可否が変わる。"""
    assert passes_criteria(inputs(ece=0.0500, ece_floor=0.0400)).adopt
    assert not passes_criteria(inputs(ece=0.0500, ece_floor=0.0300)).adopt


def test_unavailable_ece_is_noted_not_failed() -> None:
    decision = passes_criteria(inputs(ece=None, ece_floor=None))
    assert decision.adopt
    assert any("ECE" in n for n in decision.notes)


# --- 欠損率 ---

def test_null_rate_above_the_limit_fails() -> None:
    decision = passes_criteria(inputs(null_rates={"elo_diff": 0.0, "travel_km_diff": 0.31}))
    assert not decision.adopt
    assert any("travel_km_diff" in f for f in decision.failures)


def test_null_rate_exactly_at_the_limit_passes() -> None:
    assert passes_criteria(inputs(null_rates={"x": MAX_FEATURE_NULL_RATE})).adopt


# --- 分散が0の列（欠損率では捕まらない） ---

def test_known_constant_columns_are_noted_not_failed() -> None:
    """**既知で理由の説明がつくものは止めない。** 止めると工程8が進まない。"""
    decision = passes_criteria(inputs(constant_columns=sorted(KNOWN_CONSTANT)))
    assert decision.adopt
    assert len(decision.notes) == len(KNOWN_CONSTANT)
    assert all("既知" in n for n in decision.notes if "定数列" in n)


def test_unexpected_constant_column_fails() -> None:
    """`elo_diff` が定数になったモデルを「Brier が良いから」で通してはならない。"""
    decision = passes_criteria(inputs(constant_columns=["elo_diff"]))
    assert not decision.adopt
    assert any("想定外の定数列" in f for f in decision.failures)


def test_known_and_unexpected_constants_are_reported_separately() -> None:
    decision = passes_criteria(
        inputs(constant_columns=["elo_diff", *sorted(KNOWN_CONSTANT)]),
    )
    assert not decision.adopt
    assert any("elo_diff" in f for f in decision.failures)
    # 既知のぶんは注記として残る（落ちた理由と混ぜない）
    assert len(decision.notes) == len(KNOWN_CONSTANT)


def test_the_entry_keys_are_the_known_constants() -> None:
    """**`game_entries` が空であることに由来する4キーだけ**（実測で確認済み）。

    勝敗モデルの3列（2026-09-25）と、第2段 PlayerMinutes の `team_minutes_lost`
    （2026-10-03。126,931行で1種類、抜いても MAE が完全に同一）。

    **集合を固定するのは「知らないまま通ること」を防ぐためである**（4.6）。
    ここへ足すときは、定数になる理由が**設計上の既知の未実装**に由来することを
    確かめる。`gameday_update` を実装したら4つとも外す。
    """
    assert set(KNOWN_CONSTANT) == {
        "minutes_lost_diff",
        "top_players_out_diff",
        "entry_is_official",
        "team_minutes_lost",
    }
    for reason in KNOWN_CONSTANT.values():
        assert "game_entries" in reason
        assert "gameday_update" in reason


# --- 理由の提示 ---

def test_every_failed_condition_is_listed() -> None:
    """**最初に落ちた1つで打ち切らない。** 直す側は全部知りたい。"""
    decision = passes_criteria(inputs(
        brier=0.2200,
        baseline_elo_brier=0.2100,
        ece=0.1000,
        ece_floor=0.0400,
        null_rates={"x": 0.5},
        constant_columns=["elo_diff"],
        difference=Difference(point=-0.0150, ci_low=-0.0200, ci_high=-0.0100),
    ))
    assert not decision.adopt
    assert len(decision.failures) >= 5


def test_summary_says_why_it_was_rejected() -> None:
    decision = passes_criteria(inputs(baseline_elo_brier=0.1990))
    assert decision.summary.startswith("基準未達")
    assert "Elo単体" in decision.summary


def test_summary_of_an_adopted_model_carries_the_notes() -> None:
    decision = passes_criteria(inputs(constant_columns=["entry_is_official"]))
    assert decision.summary.startswith("採用基準を満たした")
    assert "entry_is_official" in decision.summary


def test_thresholds_are_not_defined_twice() -> None:
    """**閾値の出どころは `params.py` だけである。**

    `criteria.py` が自分で数値を持つと、片方だけ直したときに
    「設計どおりのはずのゲート」が静かにずれる。
    """
    import pathlib

    from batch.model import params

    body = (pathlib.Path(params.__file__).parent / "criteria.py").read_text(encoding="utf-8")
    for name in ("MIN_EVAL_N", "MIN_EFFECT", "ECE_FLOOR_K", "MAX_FEATURE_NULL_RATE"):
        assert f"{name} = " not in body, f"{name} を criteria.py で定義している"
    assert (MIN_EVAL_N, MIN_EFFECT, ECE_FLOOR_K, MAX_FEATURE_NULL_RATE) == (
        params.MIN_EVAL_N, params.MIN_EFFECT, params.ECE_FLOOR_K, params.MAX_FEATURE_NULL_RATE,
    )
