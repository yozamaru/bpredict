"""選手視点の特徴量（詳細設計 2.3 / 2.3.1）。

**1行は「1試合 × 1選手」である。** 段ごとに行の集合が違う（2.3.1）。

| 段 | 行の集合 | いま組めるか |
|---|---|---|
| 第1段 PlayerAvail | 試合 × **出場しうる選手**（`candidates()`） | **組める。本モジュールの対象** |
| 第2段 PlayerMinutes | 試合 × **出場した選手** | **組める。本モジュールの対象** |
| 第3段 PlayerRates | 同上 | **組める**（列は 2.3.1 で確定した） |

**`player_game_stats` に行があることが「出場した」の定義である。** 第2段は
`E[出場時間 | 出場]` を学習するため、この表がそのまま行の集合になり、追加の
前提を要しない。第1段の正例も同じ定義で、負例は候補のうち行がない選手である。

**候補に `player_seasons`（ロスター）を使わない。** 取得した断面は時点を持たず
（`joined_on` が NULL）、季中に加入した選手が加入前の試合の候補に現れる
（絶対ルール1）。**過去の出場実績から作る** — 詳細設計 2.3.1 に2案の実測がある。

**所属の判定は `player_game_stats.club_id`（実績）で行う**（2.1 の規約6）。
`players` の現在の所属で判定すると、移籍した選手が過去の所属クラブから消え、
**リークテストでも検出されない静かなバグ**になる。
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from batch.features import player as player_features
from batch.features import team_rate, team_strength
from batch.features.base import Context, build_context
from batch.features.constants import RECENT_WINDOWS
from batch.features.dataset import Dataset
from batch.features.errors import FeatureError
from batch.features.prepared import Prepared

#: 直近N試合の窓。**新しい定数を増やさない**（`RECENT_WINDOWS` の 5 と 10）。
SHORT_WINDOW, LONG_WINDOW = RECENT_WINDOWS

#: フリースローの攻撃終了係数。**1.3 の `possessions` と同じ値**（新しい定数を
#: 増やさない）。P0-7（日本データでの妥当性）が未測定であることも同じである
FT_PLAY_COEFFICIENT = 0.44

#: 1チームの総出場時間（5人 × 40分。要件 6.8.5。延長は仮定しない）
TEAM_MINUTES = 200.0

#: 同時に出ている人数。**USG% の分母がこれで割る**（下記 `usage`）
PLAYERS_ON_COURT = 5.0

#: 成功率3項目 → (分子, 分母)。**`team_rate.PCT_TARGETS` を正とする**
_PCT_BY_NAME = {name: (made, att) for name, made, att in team_rate.PCT_TARGETS}

#: 欠損時の既定値（2.3.1 の表）。`minutes_l5_player` は既定値を持たない
#: （欠けたら行を落とす）ため、ここには入らない。
DEFAULTS: dict[str, float] = {"is_starter_l5": 0.0, "team_minutes_lost": 0.0}

#: 第2段（PlayerMinutes）の列。順序は固定する。
MINUTES_KEYS: tuple[str, ...] = (
    "minutes_l5_player", "minutes_l10_player", "is_starter_l5", "team_minutes_lost",
)

#: 第1段（PlayerAvail）の列。順序は固定する（2.3.1）。
AVAIL_KEYS: tuple[str, ...] = (
    "minutes_l5_player", "minutes_l10_player", "games_played_ratio_l10",
    "days_since_last_played", "entry_status",
)

#: `days_since_last_played` の上限。**シーズン間（約3か月）で飽和させる。**
#: 無制限に伸びると、前季に出場して当季未出場の選手で 200日を超え、
#: **季の進行そのものを表す列になる**（`rest_days_diff` が14でクリップするのと
#: 同じ理由。2.2）。90はオフシーズンの長さにあたる
DAYS_SINCE_CAP = 90.0

#: 第1段の欠損時の既定値（2.3.1 の表）。**第2段と違い、行を落とさない** —
#: 「過去がない」ことが強い負例の signal であり、落とすと候補のうち最も
#: 出場しない層が学習から消える
AVAIL_DEFAULTS: dict[str, float] = {
    "minutes_l5_player": 0.0,
    "minutes_l10_player": 0.0,
    "games_played_ratio_l10": 0.0,
    "days_since_last_played": DAYS_SINCE_CAP,
    "entry_status": 0.0,
}


def _by_player(context: Context, club_id: str) -> dict[str, pd.DataFrame]:
    """そのクラブの過去試合を選手ごとに分け、**新しい順**に並べて返す。

    1試合に24人ぶん呼ばれるため、**クラブごとに1回だけ**グループ化する
    （`Context.cached`）。選手ごとに絞り込むと同じ切り出しを24回繰り返す。

    **並べ替えは安定ソートで行う。** 同じ日に複数試合があると順序が入力の並びで
    決まるが（2.1.1 の注記3）、索引が原順序を保つため実行ごとには変わらない。
    """
    def build() -> dict[str, pd.DataFrame]:
        rows = context.player_history(club_id, season_only=True)
        if rows.empty:
            return {}
        ordered = rows.sort_values("game_date", ascending=False, kind="stable")
        return {
            str(player_id): group
            for player_id, group in ordered.groupby("player_id", sort=False)
        }

    return context.cached(("player_rate.by_player", club_id), build)


def _recent(context: Context, club_id: str, player_id: str, window: int) -> pd.DataFrame:
    return _by_player(context, club_id).get(player_id, pd.DataFrame()).head(window)


def minutes_recent(
    context: Context, club_id: str, player_id: str, window: int,
) -> float | None:
    """その選手の直近N試合の平均出場時間。

    **シーズン境界を越えない**（2.2.1 と同じ。`winrate_recent` の規約に合わせる）。
    季の序盤は窓に届かないが、**ある分だけで集計する**。
    """
    return context.cached(
        ("player_rate.minutes_recent", club_id, player_id, window),
        lambda: _mean(_recent(context, club_id, player_id, window), "minutes"))


def starter_rate(context: Context, club_id: str, player_id: str) -> float | None:
    """直近5試合のスターター率（`started` の平均）。"""
    return context.cached(
        ("player_rate.starter_rate", club_id, player_id),
        lambda: _mean(_recent(context, club_id, player_id, SHORT_WINDOW), "started"))


def _mean(rows: pd.DataFrame, column: str) -> float | None:
    """**欠損を落としてから平均する。** 0埋めしない（規約5）。"""
    if rows.empty:
        return None
    values = rows[column].to_numpy(dtype=np.float64, na_value=np.nan)
    known = values[~np.isnan(values)]
    return None if known.size == 0 else float(known.mean())


def minutes_row(
    context: Context, club_id: str, player_id: str,
) -> dict[str, float] | None:
    """第2段（PlayerMinutes）の1行を、**できあいの `Context` から**作る。

    **1試合ぶんの選手をまとめるときは、`Context` を試合ごとに1回だけ作って
    この関数を呼ぶ。** 行ごとに `build_context` を呼ぶと、`Context` の記憶
    （`_by_player` のグループ化と `minutes_lost` の集計）が毎行捨てられる。
    1試合16人で16倍の無駄になり、実データ 146,463行では**19分かかる**
    （2026-10-03 の実測。2.1.1 で同じ罠を踏んでいる）。

    **`minutes_l5_player` が欠けたら `None` を返す**（行を落とす）。その選手の
    過去が1試合も無ければ出場時間を予測する材料が無く、既定値で埋めると
    「平均を出しているだけ」の行が学習に混ざる（2.3.1）。

    `club_id` は対象試合の出場クラブでなければならない。別のクラブを渡すのは
    呼び出し側の誤りであり、黙って計算しない。
    """
    if club_id not in (context.home_club_id, context.away_club_id):
        raise FeatureError(f"この試合に出場しないクラブ: {club_id}")

    short = minutes_recent(context, club_id, player_id, SHORT_WINDOW)
    if short is None:
        return None
    long = minutes_recent(context, club_id, player_id, LONG_WINDOW)
    if long is None:
        return None

    started = starter_rate(context, club_id, player_id)
    lost = player_features.minutes_lost(context, club_id)
    row: dict[str, float] = {
        "minutes_l5_player": short,
        "minutes_l10_player": long,
        "is_starter_l5": (
            DEFAULTS["is_starter_l5"] if started is None else started),
        "team_minutes_lost": (
            DEFAULTS["team_minutes_lost"] if lost is None else lost),
    }
    if set(row) != set(MINUTES_KEYS):
        raise ValueError(
            f"特徴量のキーが定義と一致しない: {sorted(set(row) ^ set(MINUTES_KEYS))}")
    return {key: row[key] for key in MINUTES_KEYS}


def build_minutes_features(
    game_id: str, as_of: datetime, ds: Dataset, club_id: str, player_id: str,
    prepared: Prepared | None = None,
) -> dict[str, float] | None:
    """1行だけ作る入口。**多数の行を回すときは使わない**（上記）。"""
    return minutes_row(
        build_context(game_id, as_of, ds, prepared), club_id, player_id)


# ---------------------------------------------------------------------------
# 第1段（PlayerAvail）
# ---------------------------------------------------------------------------


def candidates(context: Context, club_id: str) -> list[str]:
    """その試合でそのクラブの「出場しうる選手」。**過去の事実だけから作る。**

        当季の `finished_at <= as_of` の出場実績  ∪  前季にそのクラブで出場

    **和集合である。** 当季の実績だけに絞ると、前季の主力が当季まだ1試合も
    出ていない（負傷明けなど）場合に候補から落ちる。

    **`player_seasons`（ロスター）を使わない。** 取得した断面は時点を持たず、
    季中の加入が加入前の試合の候補に現れる（絶対ルール1）。詳細設計 2.3.1 に
    2案の実測がある — リークしない本案のほうが recall でも上回った。

    **季の1試合目は空になりうる。** 前季にそのクラブで出場した選手がいない
    （データ上の最初の季、新規参入クラブ）場合である。**空を隠さない** —
    呼び出し側が「この試合は個人スタッツを出せない」と判断する材料になる。

    並びは固定する（選手IDの昇順）。実行ごとに行の順序が変わると、
    学習の再現性（`deterministic`）が崩れる。
    """
    if club_id not in (context.home_club_id, context.away_club_id):
        raise FeatureError(f"この試合に出場しないクラブ: {club_id}")

    def build() -> list[str]:
        found: set[str] = set()
        current = context.player_history(club_id, season_only=True)
        if not current.empty:
            found |= {str(value) for value in current["player_id"]}
        previous = context.previous_season_id
        if previous is not None:
            rows = context.player_history_in_season(club_id, previous)
            if not rows.empty:
                found |= {str(value) for value in rows["player_id"]}
        return sorted(found)

    return context.cached(("player_rate.candidates", club_id), build)


def games_played_ratio(context: Context, club_id: str, player_id: str) -> float | None:
    """当季の直近10試合のうち、その選手が出場した割合。

    **分母はクラブの試合数**である（選手の出場試合数ではない）。10試合に
    届かない序盤は、ある分だけで割る（2.1.1）。
    """
    def club_games() -> list[str]:
        history = context.club_history(club_id, season_only=True)
        if history.empty:
            return []
        return [str(value) for value in history.head(LONG_WINDOW)["game_id"]]

    recent = context.cached(("player_rate.club_recent", club_id), club_games)
    if not recent:
        return None
    played = _by_player(context, club_id).get(player_id)
    if played is None or played.empty:
        return 0.0
    appeared = {str(value) for value in played["game_id"]}
    return sum(1 for game_id in recent if game_id in appeared) / len(recent)


def days_since_last_played(
    context: Context, club_id: str, player_id: str,
) -> float | None:
    """最終出場からの日数。**`DAYS_SINCE_CAP` でクリップする。**

    **そのクラブでの出場に限る**（季は跨ぐ）。候補集合がクラブ単位であるため、
    ここだけ全クラブに広げると「候補に入らない選手の値」が混ざる。移籍して
    きた選手は最初の出場まで候補に入らないため、この定義で欠落は生じない。

    全クラブに広げる案は採らない — `Context.finished_player_stats` を試合ごとに
    作ることになり、実データでは 37億行の複製になる（2.1.1）。
    """
    def build() -> dict[str, str]:
        rows = context.player_history(club_id, season_only=False)
        if rows.empty:
            return {}
        ordered = rows.sort_values("game_date", ascending=False, kind="stable")
        grouped = ordered.groupby("player_id", sort=False)["game_date"].first()
        return {str(key): str(value) for key, value in grouped.items()}

    last = context.cached(("player_rate.last_played", club_id), build).get(player_id)
    if last is None:
        return None
    gap = (pd.Timestamp(context.game_date) - pd.Timestamp(last)).days
    return float(min(max(gap, 0), DAYS_SINCE_CAP))


def avail_row(context: Context, club_id: str, player_id: str) -> dict[str, float]:
    """第1段（PlayerAvail）の1行。**`None` を返さない。**

    第2段は履歴がない行を落とすが（材料が無いため）、**第1段では「履歴がない」
    ことが強い負例の signal である**。落とすと、候補のうち最も出場しない層が
    学習から消える（2.3.1）。

    `entry_status` は**試合単位**である（2.3 の「エントリー情報（公式/推定）」）。
    選手ごとの ENTRY / OUT を列にするかは別の判断で、`gameday_update` が入って
    から決める（いま `game_entries` は0行で、この列は定数0である）。
    """
    if club_id not in (context.home_club_id, context.away_club_id):
        raise FeatureError(f"この試合に出場しないクラブ: {club_id}")

    values: dict[str, float | None] = {
        "minutes_l5_player": minutes_recent(
            context, club_id, player_id, SHORT_WINDOW),
        "minutes_l10_player": minutes_recent(
            context, club_id, player_id, LONG_WINDOW),
        "games_played_ratio_l10": games_played_ratio(context, club_id, player_id),
        "days_since_last_played": days_since_last_played(
            context, club_id, player_id),
        "entry_status": float(player_features.entry_is_official(context)),
    }
    if set(values) != set(AVAIL_KEYS):
        raise ValueError(
            f"特徴量のキーが定義と一致しない: {sorted(set(values) ^ set(AVAIL_KEYS))}")
    row: dict[str, float] = {}
    for key in AVAIL_KEYS:
        value = values[key]
        # **`or` で畳まない。** 0.0 と None を同じ枝に落とすと、`days_since_last_played`
        # の「同日に出場した（0日）」が既定値の 90日と入れ替わる
        row[key] = AVAIL_DEFAULTS[key] if value is None else float(value)
    return row


def build_avail_features(
    game_id: str, as_of: datetime, ds: Dataset, club_id: str, player_id: str,
    prepared: Prepared | None = None,
) -> dict[str, float]:
    """1行だけ作る入口。**多数の行を回すときは使わない**（`minutes_row` と同じ）。"""
    return avail_row(build_context(game_id, as_of, ds, prepared), club_id, player_id)


# ---------------------------------------------------------------------------
# 第3段（PlayerRates）
# ---------------------------------------------------------------------------

#: ポジションの符号化（詳細設計 2.3.1）。**順序を持たない整数として渡す** —
#: `categorical_feature` の指定は 4.7 のパラメータに無いため、木に分けさせる。
#: 未登録（185名 / 5.3%）は NaN（2.2.1 の既定をそのまま適用する）
POSITION_CODES: dict[str, int] = {"PG": 0, "SG": 1, "SF": 2, "PF": 3, "C": 4}

#: 第3段の共有列（14本すべてに入る。2.3.1 の表）。順序は固定する。
RATE_SHARED_KEYS: tuple[str, ...] = (
    "pred_minutes", "usage_l10", "position",
    "team_minutes_lost", "opponent_drtg", "opponent_pace",
)


def rate_matrix_keys(target: str) -> tuple[str, ...]:
    """**行列が持つ**列名。成功率は生の合計で持ち、`k` を学習時に当てる（2.3.1）。"""
    _require_rate_target(target)
    if target in _PCT_BY_NAME:
        return (
            *RATE_SHARED_KEYS,
            f"{target}_made_l10", f"{target}_att_l10",
            f"{target}_made_season", f"{target}_att_season",
            f"{target}_prior",
            f"opponent_{target}_allowed_l10",
        )
    return (
        *RATE_SHARED_KEYS,
        f"{target}_per_min_l10", f"{target}_per_min_season",
        f"opponent_{target}_allowed_l10",
    )


def rate_model_keys(target: str) -> tuple[str, ...]:
    """**モデルに渡る**列名（2.3 の表）。カウント9列 / 成功率10列。

    成功率は `{pct}_shrunk_*` を `k` から導いたうえで、`{pct}_att_l10` を
    **学習重み**としても渡す（要件 6.8.4）。
    """
    _require_rate_target(target)
    if target in _PCT_BY_NAME:
        return (
            *RATE_SHARED_KEYS,
            f"{target}_shrunk_l10", f"{target}_shrunk_season",
            f"{target}_att_l10",
            f"opponent_{target}_allowed_l10",
        )
    return rate_matrix_keys(target)


def all_rate_matrix_keys() -> tuple[str, ...]:
    """14本ぶんを1枚の表で持つための全列（重複を除き、出現順）。"""
    seen: list[str] = []
    for target in team_rate.TARGETS:
        for key in rate_matrix_keys(target):
            if key not in seen:
                seen.append(key)
    return tuple(seen)


def _require_rate_target(target: str) -> None:
    if target not in team_rate.TARGETS:
        raise FeatureError(f"目的変数が14項目にない: {target}")


def shrink(made: float, attempts: float, prior: float, k: float) -> float:
    """`(made + k × prior) / (attempts + k)`（要件 6.8.4）。

    **出力は `clip(0.01, 0.99)` に収める** — ロジット変換の定義域を確保するため
    （整合化が `logit` を通る。2.4）。
    """
    if k <= 0:
        raise FeatureError("シュリンクの k が正でない")
    value = (made + k * prior) / (attempts + k)
    return min(max(value, 0.01), 0.99)


def _levels(
    context: Context, club_id: str, player_id: str, *, season_only: bool,
) -> tuple[dict[str, float | None], dict[str, tuple[float, float] | None]]:
    """**14項目の水準を1パスで作る。**

    項目ごとに `rows[...].notna()` を回すと pandas の呼び出しが1行あたり28回に
    なり、**1行 45ms（全体で110分）かかった**（2026-10-05 の実測）。小さな表に
    対する pandas の呼び出しそのものが支配的で、集計の中身ではない。
    numpy へ1回落として項目ごとにマスクを取る（2.1.1 で踏んだのと同じ型の罠）。

    カウントは **Σカウント ÷ Σ出場時間**、成功率は **(Σ成功数, Σ試投数)**。
    どちらも「分子と分母をそれぞれ合計してから割る」規則に従う（`off_rating`）。
    """
    def build() -> tuple[
            dict[str, float | None], dict[str, tuple[float, float] | None]]:
        rows = (
            _by_player(context, club_id).get(player_id, pd.DataFrame())
            if season_only
            else _recent(context, club_id, player_id, LONG_WINDOW)
        )
        counts: dict[str, float | None] = dict.fromkeys(team_rate.COUNT_TARGETS)
        pcts: dict[str, tuple[float, float] | None] = dict.fromkeys(
            name for name, _, _ in team_rate.PCT_TARGETS)
        if rows.empty:
            return counts, pcts

        minutes = rows["minutes"].to_numpy(dtype=np.float64, na_value=np.nan)
        minutes_known = ~np.isnan(minutes)
        values = rows[list(team_rate.COUNT_TARGETS)].to_numpy(
            dtype=np.float64, na_value=np.nan)
        for position, target in enumerate(team_rate.COUNT_TARGETS):
            column = values[:, position]
            mask = minutes_known & ~np.isnan(column)
            if not mask.any():
                continue
            total = float(minutes[mask].sum())
            if total > 0:
                counts[target] = float(column[mask].sum()) / total

        columns = [c for _, made, att in team_rate.PCT_TARGETS for c in (made, att)]
        shots = rows[columns].to_numpy(dtype=np.float64, na_value=np.nan)
        for position, (name, _, _) in enumerate(team_rate.PCT_TARGETS):
            made, att = shots[:, position * 2], shots[:, position * 2 + 1]
            mask = ~np.isnan(made) & ~np.isnan(att)
            if mask.any():
                pcts[name] = (float(made[mask].sum()), float(att[mask].sum()))
        return counts, pcts

    return context.cached(
        ("player_rate.levels", club_id, player_id, season_only), build)


def per_min_level(
    context: Context, club_id: str, player_id: str, target: str, *,
    season_only: bool,
) -> float | None:
    """カウント項目の **Σカウント ÷ Σ出場時間**（2.3.1）。

    `season_only=False` のときは直近10試合（窓はシーズン境界を越えない）。
    """
    if target not in team_rate.COUNT_TARGETS:
        raise FeatureError(f"カウント11項目にない: {target}")
    return _levels(context, club_id, player_id, season_only=season_only)[0][target]


def pct_sums(
    context: Context, club_id: str, player_id: str, target: str, *,
    season_only: bool,
) -> tuple[float, float] | None:
    """成功率項目の **(Σ成功数, Σ試投数)**。縮約前の生の合計（2.3.1）。"""
    if target not in _PCT_BY_NAME:
        raise FeatureError(f"成功率3項目にない: {target}")
    return _levels(context, club_id, player_id, season_only=season_only)[1][target]


def usage(context: Context, club_id: str, player_id: str) -> float | None:
    """使用率（USG%）。**業界標準の式を採る**（運営者の判断。2.3.1）。

        攻撃終了数 = FGA + 0.44 × FTA + TOV          FGA = 2FGA + 3FGA

        usage = (本人の攻撃終了数 ÷ 本人の出場時間)
              ÷ (チームの攻撃終了数 ÷ (200分 ÷ 5))

    **係数 0.44 は 1.3 の `possessions` と同じ値を使う**（新しい定数を増やさない）。
    **出場時間で割る** — 単純なシェアでは長く出ている選手が自動的に高くなるが、
    その情報は `minutes_l5_player` が既に持っており重複する。

    窓は直近10試合。**本人かチームのどちらかの分母が0なら None**（規約5）。
    """
    rows = _recent(context, club_id, player_id, LONG_WINDOW)
    if rows.empty:
        return None
    own = _plays(rows)
    if own is None:
        return None
    own_plays, own_minutes = own
    if own_minutes <= 0:
        return None

    # **チーム側は選手に依らない。** 1試合16人で16回数え直すと、`usage` だけで
    # 1行 6ms かかる（実測）。クラブごとに1回だけ数える
    def team_rate_per_minute() -> float | None:
        history = context.club_history(club_id, season_only=True).head(LONG_WINDOW)
        team = context.stats_of(history, club_id)
        if team.empty:
            return None
        plays = _team_plays(team)
        if plays is None:
            return None
        # **分母は `Tm MP / 5` = 40分**（標準の USG% の式）。200分で割ると
        # 「平均が 1.0」になる別の量になる（最初の版が実際にそうなり、実測の
        # 平均が 0.90 だった）。5 で割れば平均が約 0.20 = 20% に落ち着く
        return plays / (TEAM_MINUTES / PLAYERS_ON_COURT * len(team))

    team_per_minute = context.cached(
        ("player_rate.team_plays_per_minute", club_id), team_rate_per_minute)
    if team_per_minute is None:
        return None
    if team_per_minute <= 0:
        return None
    return (own_plays / own_minutes) / team_per_minute


#: 攻撃終了数に要る列（FGA = 2FGA + 3FGA）。順序が重み `_PLAY_WEIGHTS` と対応する
_PLAY_COLUMNS = ("fg2a", "fg3a", "fta", "tov")
_PLAY_WEIGHTS = np.array([1.0, 1.0, FT_PLAY_COEFFICIENT, 1.0])


def _plays(rows: pd.DataFrame) -> tuple[float, float] | None:
    """選手行から (攻撃終了数の合計, 出場時間の合計)。

    **`dropna` を使わない。** 小さな表に対する pandas の呼び出しが支配的で、
    `usage` だけで 1行 6ms かかっていた（`_levels` と同じ理由）。
    """
    if rows.empty:
        return None
    block = rows[[*_PLAY_COLUMNS, "minutes"]].to_numpy(
        dtype=np.float64, na_value=np.nan)
    mask = ~np.isnan(block).any(axis=1)
    if not mask.any():
        return None
    usable = block[mask]
    plays = float((usable[:, :-1] @ _PLAY_WEIGHTS).sum())
    return plays, float(usable[:, -1].sum())


def _team_plays(rows: pd.DataFrame) -> float | None:
    """チーム行から攻撃終了数の合計。"""
    if rows.empty:
        return None
    block = rows[list(_PLAY_COLUMNS)].to_numpy(dtype=np.float64, na_value=np.nan)
    mask = ~np.isnan(block).any(axis=1)
    if not mask.any():
        return None
    return float((block[mask] @ _PLAY_WEIGHTS).sum())


def league_prior(context: Context, target: str) -> float | None:
    """リーグ全体の成功率。**当季。当季が0本のときだけ前季**（2.3.1）。

    索引（`LeagueRateIndex`）が二分探索で引く。行ごとに全クラブを集計すると
    2.1.1 の罠に戻る（18億行）。
    """
    if target not in _PCT_BY_NAME:
        raise FeatureError(f"成功率3項目にない: {target}")
    index = context.index.league_rates
    if index is None:
        return None
    return index.at(
        context.season_id,
        context.index.as_of_ns(context.as_of),
        target,
        context.previous_season_id,
    )


def position_code(context: Context, player_id: str) -> float | None:
    """ポジションを整数に符号化する。**未登録は None**（2.3.1）。

    出典は `player_seasons`（1.2）。**候補集合には使わない**（時点で絞れないため）が、
    ポジションは季を通じて変わらない属性なので時点を要しない（2.3.1）。
    """
    def build() -> dict[str, float]:
        table = context.dataset.tables.get("player_seasons")
        if table is None or table.empty:
            return {}
        rows = table[table["season_id"].astype(str) == context.season_id]
        found: dict[str, float] = {}
        for player, value in zip(rows["player_id"], rows["position"], strict=True):
            code = POSITION_CODES.get(str(value))
            if code is not None:
                found[str(player)] = float(code)
        return found

    return context.cached(("player_rate.positions",), build).get(player_id)


def rate_row(
    context: Context, club_id: str, player_id: str,
) -> dict[str, float] | None:
    """第3段（PlayerRates）の1行を、**できあいの `Context` から**作る。

    **`pred_minutes` は NaN で置く。** この列は第2段の出力であり、特徴量の段では
    まだ無い（学習時は fold ごとに第2段を当てはめて埋め、推論時はモデルの出力を
    入れる。2.3.1）。**列を落とさずに NaN を置く**のは、行列の列の集合を
    `rate_matrix_keys()` と一致させ続けるためである。

    **第2段の行が作れない選手は None を返す**（2.3.1）。出場時間を出せなければ
    カウントの水準も出せない（レートに掛ける相手が無い）。

    **成功率はシュリンク前の生の合計で持つ**（2.3.1）。`k` は学習時に探索する
    ため、行列の段では当てない。
    """
    if club_id not in (context.home_club_id, context.away_club_id):
        raise FeatureError(f"この試合に出場しないクラブ: {club_id}")
    if minutes_row(context, club_id, player_id) is None:
        return None

    opponent_id = (
        context.away_club_id if club_id == context.home_club_id
        else context.home_club_id
    )

    # **相手についての列は試合ごとに1回しか計算しない。** 1試合16人で16倍の
    # 無駄になる（`minutes_row` が `Context` を試合ごとに作る理由と同じ）。
    # `opponent_allowed` は14回呼ばれ、引数は (相手, 項目) だけに依る
    def allowed(target: str) -> float | None:
        return context.cached(
            ("player_rate.allowed", opponent_id, target),
            lambda: team_rate.opponent_allowed(context, opponent_id, target))

    row: dict[str, float] = {
        # **第2段の出力。学習時・推論時に埋める**（上記）
        "pred_minutes": float("nan"),
        "usage_l10": _or_nan(usage(context, club_id, player_id)),
        "position": _or_nan(position_code(context, player_id)),
        "team_minutes_lost": _or_default(
            player_features.minutes_lost(context, club_id), "team_minutes_lost"),
        "opponent_drtg": _or_nan(context.cached(
            ("player_rate.opp_drtg", opponent_id),
            lambda: team_strength.def_rating(context, opponent_id))),
        "opponent_pace": _or_nan(context.cached(
            ("player_rate.opp_pace", opponent_id),
            lambda: team_strength.pace(context, opponent_id))),
    }

    recent_counts, recent_pcts = _levels(
        context, club_id, player_id, season_only=False)
    season_counts, season_pcts = _levels(
        context, club_id, player_id, season_only=True)

    for target in team_rate.COUNT_TARGETS:
        row[f"{target}_per_min_l10"] = _or_nan(recent_counts[target])
        row[f"{target}_per_min_season"] = _or_nan(season_counts[target])
        row[f"opponent_{target}_allowed_l10"] = _or_nan(allowed(target))

    for target, _, _ in team_rate.PCT_TARGETS:
        recent = recent_pcts[target]
        season = season_pcts[target]
        row[f"{target}_made_l10"] = _or_nan(None if recent is None else recent[0])
        row[f"{target}_att_l10"] = _or_nan(None if recent is None else recent[1])
        row[f"{target}_made_season"] = _or_nan(None if season is None else season[0])
        row[f"{target}_att_season"] = _or_nan(None if season is None else season[1])
        row[f"{target}_prior"] = _or_nan(league_prior(context, target))
        row[f"opponent_{target}_allowed_l10"] = _or_nan(allowed(target))

    expected = set(all_rate_matrix_keys())
    if set(row) != expected:
        raise ValueError(
            f"特徴量のキーが定義と一致しない: {sorted(set(row) ^ expected)}")
    return {key: row[key] for key in all_rate_matrix_keys()}


def _or_nan(value: float | None) -> float:
    """**欠損は NaN のまま LightGBM に渡す**（2.2.1 と同じ。既定値を決め打ちしない）。"""
    return float("nan") if value is None else float(value)


def _or_default(value: float | None, key: str) -> float:
    return DEFAULTS[key] if value is None else float(value)


def build_rate_features(
    game_id: str, as_of: datetime, ds: Dataset, club_id: str, player_id: str,
    prepared: Prepared | None = None,
) -> dict[str, float] | None:
    """1行だけ作る入口。**多数の行を回すときは使わない**（`minutes_row` と同じ）。"""
    return rate_row(
        build_context(game_id, as_of, ds, prepared), club_id, player_id)
