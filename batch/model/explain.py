"""根拠のグループ集約（詳細設計 2.7 / 2.7.1、要件 6.9）。

**寄与は SHAP ではなく係数から出す。** 勝敗モデルがロジスティック回帰であるため

    logit(p) = 切片 + Σ(係数_i × 平均_i) + Σ(係数_i × (特徴量_i − 平均_i))
               └──────── base_value ────────┘  └──────── 寄与の和 ────────┘

が**ログオッズ空間の厳密な恒等式**になる。`shap` パッケージは依存に加えない
（CLAUDE.md「依存を追加するとき」）。

**画面に出すのはグループ単位である**（60〜70列の個別名を出さないため）。
`label_ja` と `value_text` は**そのグループで最も寄与が大きかった1項目の事実**から
作る（運営者の判断。2026-10-05）。
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from batch.features.builder import FEATURE_KEYS


class ExplainError(RuntimeError):
    """根拠を組めない。**例外に本文を入れない**（絶対ルール4）。"""


#: 要因グループ。**この順序が同点のときの並び順になる**（実行ごとに変わらないように）。
GROUP_ORDER = ("TEAM_STRENGTH", "SCHEDULE", "PLAYER", "VENUE")


@dataclass(frozen=True)
class Wording:
    """1列ぶんの文言（詳細設計 2.7.1 の文言表）。

    `label` は代表に選ばれたときの `label_ja`、`render` は `value_text` を作る。
    **向きを書かない** — 画面は `favors` を別に出すため二重になる。
    """

    group: str
    label: str
    render: Callable[[float], str]
    #: 代表に選んでよいか。**データの状態を表す列は選ばない**（下記 `entry_is_official`）。
    representative: bool = True
    #: **値の符号が「どちらが大きいか」を表すか**（詳細設計 2.7.2）。
    #:
    #: `_amount`（差の列）は表す。`_level`（片側や両チーム共通の水準）と
    #: `_choice`（選択肢）は表さない — `series_game_no` の「2戦目」は両チームに
    #: 共通で、`away_streak_away` はアウェイ側だけの値である。
    #:
    #: **「有利な側」ではない。** 係数が負の列（`drtg_diff`）では両者が逆を向く。
    directional: bool = True


def _amount(unit: str, digits: int = 0, scale: float = 1.0) -> Callable[[float], str]:
    """差の大きさ（絶対値）に単位を付ける。**符号を出さない。**

    係数が負の列では差の向きと有利な側が逆になる（`drtg_diff`）。符号をそのまま
    出すと、読者は「差が正ならホーム有利」と読む。
    """

    def render(value: float) -> str:
        return f"{abs(value) * scale:.{digits}f}{unit}"

    return render


def _level(unit: str, digits: int = 0) -> Callable[[float], str]:
    """水準そのもの（差ではない列）。絶対値を取らない。"""

    def render(value: float) -> str:
        return f"{value:.{digits}f}{unit}"

    return render


def _choice(when_true: str, when_false: str) -> Callable[[float], str]:
    def render(value: float) -> str:
        return when_true if value != 0.0 else when_false

    return render


#: 詳細設計 2.7.1 の文言表。**キーの集合は `FEATURE_KEYS` と一致させる**（下記の検査）。
#:
#: ラベルに「Elo」を出さない（専門用語であり、2.7 の表示例も「チーム力の差」と書く）。
#: `%` は勝率専用に予約してあるため、勝率や率の**差**は「ポイント」で書く（要件 8.3）。
WORDING: dict[str, Wording] = {
    # --- チーム力 ---
    "elo_diff": Wording("TEAM_STRENGTH", "チーム力の差", _amount("ポイント")),
    "elo_home": Wording(
        "TEAM_STRENGTH", "ホームのチーム力", _level("ポイント"), directional=False),
    "elo_away": Wording(
        "TEAM_STRENGTH", "アウェイのチーム力", _level("ポイント"), directional=False),
    "winrate_l5_diff": Wording(
        "TEAM_STRENGTH", "直近5試合の勝率の差", _amount("ポイント", scale=100.0)),
    "winrate_l10_diff": Wording(
        "TEAM_STRENGTH", "直近10試合の勝率の差", _amount("ポイント", scale=100.0)),
    "winrate_season_diff": Wording(
        "TEAM_STRENGTH", "今季の勝率の差", _amount("ポイント", scale=100.0)),
    "margin_l5_diff": Wording(
        "TEAM_STRENGTH", "直近5試合の平均得点差の開き", _amount("点", digits=1)),
    "margin_season_diff": Wording(
        "TEAM_STRENGTH", "今季の平均得点差の開き", _amount("点", digits=1)),
    "ortg_diff": Wording(
        "TEAM_STRENGTH", "攻撃効率の差", _amount("点（100回の攻撃あたり）", digits=1)),
    "drtg_diff": Wording(
        "TEAM_STRENGTH", "守備効率の差", _amount("点（100回の攻撃あたり）", digits=1)),
    "tov_rate_diff": Wording(
        "TEAM_STRENGTH", "ターンオーバー率の差", _amount("ポイント", digits=1, scale=100.0)),
    "oreb_rate_diff": Wording(
        "TEAM_STRENGTH", "オフェンスリバウンド率の差",
        _amount("ポイント", digits=1, scale=100.0)),
    "sos_diff": Wording(
        "TEAM_STRENGTH", "対戦してきた相手の強さの差", _amount("ポイント")),
    # --- 日程・疲労 ---
    "series_game_no": Wording(
        "SCHEDULE", "同一カードの連戦", _level("戦目"), directional=False),
    "prev_result_diff": Wording(
        "SCHEDULE", "前戦の勝敗", _choice("勝敗が分かれた", "勝敗が同じ"),
        directional=False),
    "prev_margin_diff": Wording("SCHEDULE", "前戦の得点差の開き", _amount("点")),
    "rest_days_diff": Wording("SCHEDULE", "休養日数の差", _amount("日")),
    "away_streak_away": Wording(
        "SCHEDULE", "アウェイの連続アウェイ", _level("試合"), directional=False),
    # --- 選手 ---
    "minutes_lost_diff": Wording(
        "PLAYER", "欠場者の出場時間の差", _amount("分", digits=1)),
    "top_players_out_diff": Wording("PLAYER", "主力の欠場者数の差", _amount("名")),
    # **代表に選ばない。** 出場選手が発表されているかは「データの状態」であって、
    # どちらのチームに有利でもない。寄与はグループ合計に入れるが文言には出さない
    "entry_is_official": Wording(
        "PLAYER", "出場選手の発表", _choice("発表済み", "未発表"),
        representative=False, directional=False),
}


@dataclass(frozen=True)
class Reason:
    """`prediction_reasons` の1行（詳細設計 1.5）。"""

    rank: int
    group_key: str
    label_ja: str
    value_text: str
    favors: str
    contribution: float
    base_value: float


@dataclass(frozen=True)
class Explainer:
    """係数・切片・学習データの平均。**推論ループの外で1回だけ作る**（2.7）。"""

    features: tuple[str, ...]
    intercept: float
    coefficients: Any
    means: Any

    def __post_init__(self) -> None:
        size = len(self.features)
        for name, array in (("係数", self.coefficients), ("平均", self.means)):
            values = np.asarray(array, dtype=np.float64)
            if values.ndim != 1 or values.size != size:
                raise ExplainError(f"{name}の数と特徴量の数が合わない")
            if not np.isfinite(values).all():
                raise ExplainError(f"{name}に有限でない値がある")

    @property
    def base_value(self) -> float:
        """`切片 + Σ(係数 × 平均)`。**平均的な試合のログオッズ**である。"""
        coefficients = np.asarray(self.coefficients, dtype=np.float64)
        means = np.asarray(self.means, dtype=np.float64)
        return float(self.intercept + float(coefficients @ means))

    def contributions(self, features: Mapping[str, float]) -> dict[str, float]:
        """列ごとの寄与（ログオッズ空間）。**和 + `base_value` がログオッズである。**"""
        missing = [k for k in self.features if k not in features]
        if missing:
            raise ExplainError(f"特徴量が足りない: {len(missing)}列")
        coefficients = np.asarray(self.coefficients, dtype=np.float64)
        means = np.asarray(self.means, dtype=np.float64)
        values = np.asarray([float(features[k]) for k in self.features], dtype=np.float64)
        if not np.isfinite(values).all():
            raise ExplainError("特徴量に有限でない値がある")
        return dict(zip(self.features, coefficients * (values - means), strict=True))

    def reasons(self, features: Mapping[str, float]) -> list[Reason]:
        """グループ単位の根拠を、寄与の絶対値の降順で返す。

        **合計が 0 のグループは行を作らない**（向きが定まらず `favors` を書けない）。
        閾値は置かない — 定数列の寄与は厳密に 0.0 である（2.7.1）。
        """
        per_column = self.contributions(features)
        base = self.base_value
        rows: list[tuple[float, str, float]] = []
        for group in GROUP_ORDER:
            columns = [k for k in self.features if WORDING[k].group == group]
            total = float(sum(per_column[k] for k in columns))
            if total == 0.0:
                continue
            rows.append((abs(total), group, total))

        reasons: list[Reason] = []
        for rank, (_, group, total) in enumerate(
            sorted(rows, key=lambda r: (-r[0], GROUP_ORDER.index(r[1]))), start=1
        ):
            key = _representative(self.features, per_column, group, total)
            wording = WORDING[key]
            reasons.append(Reason(
                rank=rank,
                group_key=group,
                label_ja=wording.label,
                value_text=wording.render(float(features[key])),
                favors="HOME" if total > 0 else "AWAY",
                contribution=total,
                base_value=base,
            ))
        return reasons

    def factors(self, features: Mapping[str, float]) -> list[Factor]:
        """**この予測に使った項目を全部返す**（詳細設計 2.7.2）。

        運営者の指摘「どの項目からこの予測を導き出したのかを詳しく知りたい」に
        対するもので、`reasons()` とは別物である。

        **並びは要因グループの順 → 列の順で固定する。** 寄与の大きさで並べると
        「どれがどれだけ効いたか」を主張することになり、この表が避けている話に
        戻る（2.7.2）。

        **1件も落とさない。** 定数列（寄与が厳密に 0）も「見た項目」であり、
        `entry_is_official` の「未発表」は読者にとって意味のある事実である。
        """
        missing = [k for k in self.features if k not in features]
        if missing:
            raise ExplainError(f"特徴量が足りない: {len(missing)}列")
        rows: list[Factor] = []
        for group in GROUP_ORDER:
            for key in self.features:
                wording = WORDING[key]
                if wording.group != group:
                    continue
                value = float(features[key])
                if not math.isfinite(value):
                    raise ExplainError("特徴量に有限でない値がある")
                rows.append(Factor(
                    rank=len(rows) + 1,
                    group_key=group,
                    label_ja=wording.label,
                    value_text=wording.render(value),
                    larger=_larger(wording, value),
                ))
        return rows


def _larger(wording: Wording, value: float) -> str | None:
    """値が大きい側。**「有利な側」ではない**（詳細設計 2.7.2）。

    向きを持たない列（片側の水準・両チーム共通・選択肢）と、差がちょうど 0 の
    列は None を返す — **どちらが大きいとも言えない**のであって、
    「同じだからホーム」のような既定値を置かない。
    """
    if not wording.directional or value == 0.0:
        return None
    return "HOME" if value > 0 else "AWAY"


@dataclass(frozen=True)
class Factor:
    """この予測に使った項目1つ（`prediction_factors` の1行。詳細設計 2.7.2）。

    **寄与ではない。** `Reason` が「なぜそうなったか」を要因グループに集約して
    述べるのに対し、こちらは「**何を見たか**」を列挙する。有利不利を主張しない
    ため打ち消しが起きず、**21列すべてを出せる**。
    """

    rank: int
    group_key: str
    label_ja: str
    value_text: str
    #: 値が大きい側。**「有利な側」ではない**（係数が負の列では逆を向く）。
    #: 向きを持たない列・差が 0 の列は None
    larger: str | None


def _representative(
    features: Sequence[str],
    per_column: Mapping[str, float],
    group: str,
    total: float,
) -> str:
    """グループ合計と**同じ向き**で絶対値が最大の列。

    向きを問わずに最大を採ると、グループが「ホーム有利」なのに代表の事実が逆を
    向く行が出る。合計が 0 でなければ同じ向きの列は必ず1つ以上あるが、
    `representative=False` の列しか残らないことはありうるため、そのときは
    向きを問わない最大へ落とす。
    """
    candidates = [
        k for k in features
        if WORDING[k].group == group and WORDING[k].representative
    ]
    if not candidates:
        raise ExplainError(f"代表にできる列がない: {group}")
    aligned = [k for k in candidates if per_column[k] * total > 0]
    pool = aligned or candidates
    return max(pool, key=lambda k: (abs(per_column[k]), -features.index(k)))


def logit(probability: float) -> float:
    """ログオッズ。**寄与の和と突き合わせる検算に使う。**"""
    if not 0.0 < probability < 1.0:
        raise ExplainError("確率が開区間 (0,1) に入っていない")
    return math.log(probability / (1.0 - probability))


def payload_of(reason: Reason) -> dict[str, Any]:
    """`POST /internal/predictions` の `reasons` の1要素（詳細設計 3.4）。"""
    return {
        "rank": reason.rank,
        "groupKey": reason.group_key,
        "labelJa": reason.label_ja,
        "valueText": reason.value_text,
        "favors": reason.favors,
        "contribution": reason.contribution,
        "baseValue": reason.base_value,
    }


def factor_payload(factor: Factor) -> dict[str, Any]:
    """`POST /internal/predictions` の `factors` の1要素（詳細設計 3.4）。

    **`favors` ではなく `larger` を送る。** 値が大きい側であって有利な側ではない。
    """
    return {
        "rank": factor.rank,
        "groupKey": factor.group_key,
        "labelJa": factor.label_ja,
        "valueText": factor.value_text,
        "larger": factor.larger,
    }


# **文言表と特徴量の集合が食い違ったまま動かない。** 列を足して文言を忘れると
# グループ集約が静かに取りこぼす（`WORDING[k]` が KeyError になるのは実行時であり、
# 本番の推論で初めて落ちる）。取り込み時に突き合わせる。
if set(WORDING) != set(FEATURE_KEYS):
    raise ExplainError("文言表と特徴量の集合が一致しない")
