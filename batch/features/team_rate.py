"""TeamRate（チーム目標モデル）の特徴量（詳細設計 2.2.1）。

**1行は「1試合 × 1クラブ」である。** 勝敗モデル（`builder.py`）の1行は1試合で
ホーム視点に固定されるが、ここでは1試合から2行できる。帰結は
**`diff` に集約しないこと**である（目標値は片側のチームについての量であり、
差では作れない）。

**`is_home` は特徴量にしない（2026-10-03 の実測で落とした）。** クラブ視点では
1 と 0 の両方が現れるため定数列ではなく、当初は列に入れていた。しかし14本すべてで
測ったところ**効果が検出できなかった** — 抜いた方が8本で良く、MAE の変化は
中央値 −0.040%。**ホームアドバンテージは整合化の前段（2.4）で予想スコア
（Margin 由来）から入る**ため、ここで持つ必要がない。

**列は目的変数ごとに絞る。** 共有4列 ＋ 目的変数ごとの2列 = 1モデル6列。
14本すべてに同じ70列を与えない（工程8の実測が「列を増やすと薄まる」と言っている。
`verification/RESULTS.md`）。

2.1 の規約はすべて適用される。参照は `Context.club_history` だけを通し、
**新しい絞り込みを書かない**（2.1.1）。
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from batch.features import schedule_ctx, team_strength
from batch.features.base import Context, build_context
from batch.features.constants import RECENT_WINDOWS
from batch.features.dataset import Dataset
from batch.features.errors import FeatureError
from batch.features.prepared import Prepared

#: 直近N試合の窓。**新しい定数を増やさない** — `RECENT_WINDOWS` の 10 を使う
#: （2.3 が個人の同種の列を `l10` と命名している）。
WINDOW = RECENT_WINDOWS[1]

#: カウント11項目。**`batch/model/reconcile.py` の `ATTEMPTS + COUNTS` と同じ集合。**
#: 層をまたいで共有しない（`constants.py` の `FTA_COEFFICIENT` と同じ方針）。
#: 一致は `test_targets_match_reconcile` が固定する。
COUNT_TARGETS: tuple[str, ...] = (
    "fg2a", "fg3a", "fta",
    "oreb", "dreb", "ast", "tov", "stl", "blk", "pf", "fd",
)

#: 成功率3項目と、その分子・分母の列。`reconcile.PCTS` と同じ対応。
PCT_TARGETS: tuple[tuple[str, str, str], ...] = (
    ("fg2_pct", "fg2m", "fg2a"),
    ("fg3_pct", "fg3m", "fg3a"),
    ("ft_pct", "ftm", "fta"),
)

#: 予測対象14項目（カウント11 + 成功率3）。
TARGETS: tuple[str, ...] = COUNT_TARGETS + tuple(p for p, _, _ in PCT_TARGETS)

#: 欠損時の既定値（詳細設計 2.2.1 の表）。`pace_*` は既定値を持たない
#: （欠けたら行を落とす）ため、ここには入らない。
DEFAULTS: dict[str, float] = {"rest_days_own": 0.0}

_PCT_BY_NAME = {name: (made, attempt) for name, made, attempt in PCT_TARGETS}


def shared_keys() -> tuple[str, ...]:
    """14本すべてに入る共有列。順序は固定する。

    **`is_home` は入れない**（上記。実測で効果が検出できなかった）。
    `rest_days_own` は残す — 抜くと MAE が中央値 +0.138% 悪化した。
    """
    return ("pace_own", "pace_opp", "rest_days_own")


def feature_keys(target: str) -> tuple[str, ...]:
    """ある目的変数の列名。**目的変数を列名に含める。**

    14本で同じ列名にしない — `model_versions.feature_list` が自己説明になり、
    どのモデルが何を見ているかが後から分かる。
    """
    _require_target(target)
    return (*shared_keys(), f"own_{target}_l10", f"opponent_{target}_allowed_l10")


def _require_target(target: str) -> None:
    if target not in TARGETS:
        raise FeatureError(f"目的変数が14項目にない: {target}")


def _aggregate(rows: pd.DataFrame, target: str) -> float | None:
    """`rows` から当該項目の水準を作る。**欠損時は None**（0埋めしない。規約5）。

    カウントは平均、成功率は **Σ成功数 ÷ Σ試投数**。後者は `off_rating` が定めた
    「分子と分母をそれぞれ合計してから割る」規則に従う（試合ごとの率を平均しない）。

    **シュリンクを入れない。** `k` が設計文書に定義されておらず、チームは10試合で
    約850本の試投があるため `k` が何であれ結果はほぼ動かない（詳細設計 2.2.1）。
    """
    if rows.empty:
        return None
    if target in _PCT_BY_NAME:
        made_column, attempt_column = _PCT_BY_NAME[target]
        usable = rows[rows[made_column].notna() & rows[attempt_column].notna()]
        if usable.empty:
            return None
        attempts = float(usable[attempt_column].sum())
        if attempts <= 0:
            return None
        return float(usable[made_column].sum()) / attempts
    values = rows[target].dropna()
    return None if values.empty else float(values.mean())


def own_level(context: Context, club_id: str, target: str) -> float | None:
    """自クラブの直近10試合における当該項目の水準。

    **シーズン境界を越えない**（`winrate_recent` と同じ規約。詳細設計 6.4 の
    `test_winrate_l5_does_not_cross_season_boundary` が固定している）。季の序盤は
    10試合に届かないが、**ある分だけで集計する**（2.1.1）。
    """
    _require_target(target)
    history = context.club_history(club_id, season_only=True).head(WINDOW)
    return _aggregate(context.stats_of(history, club_id), target)


def opponent_allowed(context: Context, opponent_id: str, target: str) -> float | None:
    """**相手**が直近10試合で**許した**当該項目の水準。

    相手クラブの直近10試合を引き、その各試合における**相手の相手**（= その試合の
    対戦相手）の行を集計する。`Context.stats_of_opponents` がそれを行う。
    """
    _require_target(target)
    history = context.club_history(opponent_id, season_only=True).head(WINDOW)
    return _aggregate(context.stats_of_opponents(history), target)


def all_feature_keys() -> tuple[str, ...]:
    """1行が持つ列すべて（共有3 + 目的変数ごと2 × 14 = 31）。

    **1つの `Context` から14本ぶんをまとめて作る。** 目的変数ごとに `Context` を
    組み直すと14倍かかり、しかも推論側も14項目すべてを要る（整合化の目標値は
    14項目で1組である。2.4）。**選ぶのは `features_for()` の仕事**にする。
    """
    keys = list(shared_keys())
    for target in TARGETS:
        keys += [f"own_{target}_l10", f"opponent_{target}_allowed_l10"]
    return tuple(keys)


def features_for(row: dict[str, float], target: str) -> dict[str, float]:
    """1行から、ある目的変数の6列だけを取り出す（2.2.1 の案C）。"""
    return {key: row[key] for key in feature_keys(target)}


def team_rate_row(
    context: Context, club_id: str,
) -> dict[str, float] | None:
    """1行（試合 × クラブ）を、**できあいの `Context` から**作る。

    返すのは**14本ぶんの列をまとめた1行**である（`all_feature_keys()`）。
    モデル1本に渡すのは `features_for(row, target)` の6列だけ。

    **`pace_own` / `pace_opp` が欠けたら `None` を返す**（行を落とす）。テンポが
    分からなければカウントの水準が決まらず、既定値で埋めると目的変数の分散の
    大半を説明できない行が学習に混ざる（詳細設計 2.2.1）。

    **推論は `Context` を試合ごとに1回だけ作ってここを呼ぶ。** ホームと
    アウェイで2回呼ぶため、`build_context` を内側に置くと2倍かかる
    （`minutes_row` が同じ形をしている理由と同じ。2.3.1）。

    `club_id` は対象試合の出場クラブでなければならない。別のクラブを渡すのは
    呼び出し側の誤りであり、黙って計算しない。
    """
    if club_id == context.home_club_id:
        opponent_id = context.away_club_id
    elif club_id == context.away_club_id:
        opponent_id = context.home_club_id
    else:
        raise FeatureError(f"この試合に出場しないクラブ: {club_id}")

    pace_own = team_strength.pace(context, club_id)
    pace_opp = team_strength.pace(context, opponent_id)
    if pace_own is None or pace_opp is None:
        return None

    rest = schedule_ctx.rest_days(context, club_id)
    row: dict[str, float] = {
        "pace_own": float(pace_own),
        "pace_opp": float(pace_opp),
        "rest_days_own": (
            DEFAULTS["rest_days_own"] if rest is None else float(rest)
        ),
    }
    for target in TARGETS:
        # **欠損は NaN のまま LightGBM に渡す。** 既定値が設計文書にないため
        # 決め打ちで埋めない（詳細設計 2.2.1）。LightGBM は欠損を分割に使える
        row[f"own_{target}_l10"] = _as_float(own_level(context, club_id, target))
        row[f"opponent_{target}_allowed_l10"] = _as_float(
            opponent_allowed(context, opponent_id, target))

    expected = set(all_feature_keys())
    if set(row) != expected:
        raise ValueError(f"特徴量のキーが定義と一致しない: {sorted(set(row) ^ expected)}")
    return {key: row[key] for key in all_feature_keys()}


def build_team_rate_features(
    game_id: str, as_of: datetime, ds: Dataset, club_id: str,
    prepared: Prepared | None = None,
) -> dict[str, float] | None:
    """1行だけ作る入口。**多数の行を回すときは使わない**（`team_rate_row` を使う）。"""
    return team_rate_row(build_context(game_id, as_of, ds, prepared), club_id)


def _as_float(value: float | None) -> float:
    return float("nan") if value is None else float(value)
