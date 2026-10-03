"""モデルの採用基準（詳細設計 4.6）。

**判定は純粋な関数にする。** 入力は測り終えた数値だけで、通信もファイル読み書きも
しない。学習ジョブは数値を集めてここへ渡し、戻ってきた理由をログに出す。

**真偽値だけを返さない。** 「基準未達のため有効化しない」とだけログに出ると、
どの条件で落ちたのかが後から分からない。満たした条件と落ちた条件の両方を残す。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from batch.model.metrics import Difference

# **閾値をここで定義しない。** `batch/model/params.py` が唯一の出どころである
# （同ファイルの冒頭「値の出どころを1か所にまとめる」）。2箇所に置くと、
# 片方だけ直したときに「設計どおりのはずのゲート」が静かにずれる。
from batch.model.params import (
    ECE_FLOOR_K,
    MAX_FEATURE_NULL_RATE,
    MIN_EFFECT,
    MIN_EVAL_N,
)

__all__ = [
    "ECE_FLOOR_K",
    "KNOWN_CONSTANT",
    "MAX_FEATURE_NULL_RATE",
    "MIN_EFFECT",
    "MIN_EVAL_N",
    "CriteriaError",
    "Decision",
    "Inputs",
    "passes_criteria",
]

#: **分散が0でも採用を止めない列**と、その理由。
#:
#: 定数列そのものは予測を壊さない（LightGBM は分割に使えない）。止めるべきなのは
#: 「**知らないまま**通ること」であって、既知で理由の説明がつくものではない。
#: 一方、ここに無い列が定数になったのは上流の欠陥であり、**採用を止める**
#: （`elo_diff` が定数になったモデルを「Brier が良いから」で通してはならない）。
#:
#: **`gameday_update` を実装したらこの3つを外す。** 外し忘れると、エントリーを
#: 取得しているのに定数0のままという状態を見逃す。
KNOWN_CONSTANT: Mapping[str, str] = {
    "minutes_lost_diff": "game_entries が空（gameday_update が未実装）",
    "top_players_out_diff": "game_entries が空（gameday_update が未実装）",
    "entry_is_official": "game_entries が空（gameday_update が未実装）",
    # 第2段 PlayerMinutes の列。同じ `minutes_lost` を読むため同じ理由で定数になる。
    # 実データ 126,931行で**1種類（全件0）**、抜いても MAE が完全に同一だった
    # （2026-10-03 の実測）。`gameday_update` を実装したら外す
    "team_minutes_lost": "game_entries が空（gameday_update が未実装）",
}


class CriteriaError(ValueError):
    """判定に必要な数値が揃っていない。"""


@dataclass(frozen=True)
class Inputs:
    """判定に使う数値。**測る側が揃えてから渡す。**"""

    n: int
    brier: float
    baseline_elo_brier: float
    null_rates: Mapping[str, float]
    constant_columns: Sequence[str]
    ece: float | None = None
    ece_floor: float | None = None
    #: 現行モデルを**同一の評価ウィンドウで再評価した** Brier。
    #: None は現行モデルがない（初回登録）ことを意味する
    current_brier: float | None = None
    #: 試合ごとの Brier 差のブートストラップ。初回登録では None
    difference: Difference | None = None


@dataclass(frozen=True)
class Decision:
    adopt: bool
    #: 落ちた条件。1件でもあれば `adopt` は False
    failures: list[str] = field(default_factory=list)
    #: 落ちてはいないが、残しておくべきこと（既知の定数列など）
    notes: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if self.adopt:
            tail = f"（注記: {' / '.join(self.notes)}）" if self.notes else ""
            return f"採用基準を満たした{tail}"
        return f"基準未達: {' / '.join(self.failures)}"


def passes_criteria(inputs: Inputs) -> Decision:
    """採用の可否を決める（詳細設計 4.6）。

    **Accuracy は一切使わない。** 差が統計的に識別できない（要件 6.5）。

    **`n < 500` では比較そのものを行わない。** 他の条件も見ずに現行を継続する —
    下限を満たさない標本で条件を数えると、「4つ満たして1つ落ちた」のような
    読み方を誘う。
    """
    if inputs.n < MIN_EVAL_N:
        return Decision(
            adopt=False,
            failures=[f"評価サンプル数が {inputs.n} で下限 {MIN_EVAL_N} に届かない"],
        )

    failures: list[str] = []
    notes: list[str] = []

    # 1. ベースライン（Elo単体ロジスティック回帰）を上回る。
    #    **これを超えられなければ LightGBM を使う理由がない**（要件 6.4）
    if not inputs.brier < inputs.baseline_elo_brier:
        failures.append(
            f"Brier {inputs.brier:.4f} が Elo単体 {inputs.baseline_elo_brier:.4f} を上回らない"
        )

    # 2. 現行モデルとの比較。**同一の評価ウィンドウで再評価した値**を使う
    #    （`model_versions.cv_brier` は学習当時の値であり比較にならない）
    if inputs.current_brier is None:
        # 初回登録。比較する相手がいない条件は課せない
        notes.append("現行モデルがないため Brier の比較と有意性の検査は行わない")
    else:
        if not inputs.brier < inputs.current_brier:
            failures.append(
                f"Brier {inputs.brier:.4f} が現行 {inputs.current_brier:.4f} を上回らない"
            )
        gain = inputs.current_brier - inputs.brier
        if gain < MIN_EFFECT:
            failures.append(f"Brier の改善 {gain:.4f} が最小実質差 {MIN_EFFECT} に届かない")
        if inputs.difference is None:
            raise CriteriaError("現行モデルがあるのにブートストラップの結果がない")
        if not inputs.difference.significant:
            failures.append(
                "Brier 差の95%信頼区間が0を跨ぐ"
                f"（{inputs.difference.ci_low:+.4f}, {inputs.difference.ci_high:+.4f}）"
            )

    # 3. 較正。**閾値はモデル自身の予測分布から毎回推定する**（固定値を持たない）
    if inputs.ece is None or inputs.ece_floor is None:
        # ビンあたり50件を満たせないと ECE 自体が計算できない。
        # n >= 500 を通っているので、ここに来るのは分布が偏った場合である
        notes.append("ECE を算出できなかったため較正の検査を行わない")
    else:
        limit = inputs.ece_floor * ECE_FLOOR_K
        if not inputs.ece < limit:
            failures.append(
                f"ECE {inputs.ece:.4f} がノイズフロア由来の閾値 {limit:.4f} を下回らない"
            )

    # 4. 特徴量の欠損率
    worst = max(inputs.null_rates.items(), key=lambda kv: kv[1], default=None)
    if worst is not None and worst[1] > MAX_FEATURE_NULL_RATE:
        failures.append(f"欠損率が {MAX_FEATURE_NULL_RATE:.0%} を超える特徴量がある（{worst[0]}）")

    # 5. 分散が0の列。**欠損率では捕まらない**（NULL ではなく定数のため）
    unexpected = [c for c in inputs.constant_columns if c not in KNOWN_CONSTANT]
    known = [c for c in inputs.constant_columns if c in KNOWN_CONSTANT]
    if unexpected:
        failures.append(f"想定外の定数列がある（{' / '.join(sorted(unexpected))}）")
    for name in sorted(known):
        notes.append(f"定数列（既知）: {name} — {KNOWN_CONSTANT[name]}")

    return Decision(adopt=not failures, failures=failures, notes=notes)
