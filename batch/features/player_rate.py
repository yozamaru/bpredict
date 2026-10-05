"""選手視点の特徴量（詳細設計 2.3 / 2.3.1）。

**1行は「1試合 × 1選手」である。** 段ごとに行の集合が違う（2.3.1）。

| 段 | 行の集合 | いま組めるか |
|---|---|---|
| 第1段 PlayerAvail | 試合 × **出場しうる選手**（`candidates()`） | **組める。本モジュールの対象** |
| 第2段 PlayerMinutes | 試合 × **出場した選手** | **組める。本モジュールの対象** |
| 第3段 PlayerRates | 同上 | **組めない**（`k` / `prior` / `usage_l10` が未定義） |

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

import pandas as pd

from batch.features import player as player_features
from batch.features.base import Context, build_context
from batch.features.constants import RECENT_WINDOWS
from batch.features.dataset import Dataset
from batch.features.errors import FeatureError
from batch.features.prepared import Prepared

#: 直近N試合の窓。**新しい定数を増やさない**（`RECENT_WINDOWS` の 5 と 10）。
SHORT_WINDOW, LONG_WINDOW = RECENT_WINDOWS

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
    rows = _recent(context, club_id, player_id, window)
    if rows.empty:
        return None
    values = rows["minutes"].dropna()
    return None if values.empty else float(values.mean())


def starter_rate(context: Context, club_id: str, player_id: str) -> float | None:
    """直近5試合のスターター率（`started` の平均）。"""
    rows = _recent(context, club_id, player_id, SHORT_WINDOW)
    if rows.empty:
        return None
    values = rows["started"].dropna()
    return None if values.empty else float(values.mean())


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
