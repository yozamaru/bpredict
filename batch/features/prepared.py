"""特徴量生成の前処理を1回だけ行う（工程8の性能。基本設計 2.2）。

**絞り込みの意味は変えない。** 変えるのは「いつ計算するか」だけである。
`as_of` による絞り込みは `batch/features/base.py` の1か所に残り、ここは
その絞り込みを速くするための索引を持つ。

**なぜ必要か。** 6,270試合の特徴量生成に43分かかっていた（2026-10-02 の実測）。
内訳は同じ前処理の繰り返しである。

| 繰り返していたもの | 1試合ごと | 全体 |
|---|---|---|
| `finished_at` の時刻パース | 12,540行 | **7,800万回** |
| `player_game_stats` の `isin` | 146,463行 × 4回 | **37億行**。最大の山 |
| `club_history` の真偽マスク | 12,540行 × 17回 | — |
| `team_ratings` の走査と並べ替え | 12,532行 × 2回 | **1億5千万行** |

**原順序を保つ。** `_recent_minutes` は `game_date` の降順に並べて選手ごとに
先頭N件を取るため、**同じ日の試合が複数あると入力の並びで結果が変わる**。
並べ替えると特徴量の値が静かに変わる。
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

import numpy as np
import pandas as pd
from numpy.typing import NDArray

from batch.features.dataset import Dataset
from batch.features.errors import FeatureError

type Times = NDArray[np.int64]
type Positions = NDArray[np.intp]

#: `finished_at` が NULL の行の時刻。**どの `as_of` よりも後**になるため、
#: 「終了していない試合を集計に含めない」という規約が比較だけで満たされる
NEVER = np.iinfo(np.int64).max

#: 時刻を整数で比べるときの単位。**両辺をここで固定する。**
#: `pd.to_datetime` が返す解像度は入力で変わる（pandas 3 では秒・マイクロ秒に
#: なりうる）ため、`astype("int64")` の値の単位も変わる。**片方だけがナノ秒だと
#: 比較が静かに全件通る** — 実際に `as_of` の絞り込みが効かず、1試合目に
#: 過去試合が見えていた（2026-10-02）。
UNIT: Literal["s", "ms", "us", "ns"] = "ns"

#: `team_games` に無い試合の選手行が持つシーズン。**どのシーズンにも一致しない。**
#: 元の実装はこの行を当季の集計から落としていた（`season_game_ids` に入らない）
_NO_SEASON = "\x00"


def _utc_ns(series: pd.Series) -> Times:
    """ISO 8601 の文字列を int64 の `UNIT` へ。NULL は `NEVER`。"""
    parsed = pd.to_datetime(series, utc=True, format="ISO8601", errors="coerce")
    values = parsed.dt.as_unit(UNIT).astype("int64").to_numpy(dtype=np.int64, copy=True)
    return np.where(parsed.isna().to_numpy(), NEVER, values)


def _group(
    frame: pd.DataFrame, clubs: pd.Series | None, seasons: pd.Series | None,
    order: Positions | None = None,
) -> tuple[dict[str, Positions], dict[tuple[str, str], Positions]]:
    """クラブ別・(クラブ, シーズン)別の行位置。

    `order` を渡すとその並びで、省略すると**原順序**（昇順）で持つ。
    グループ分けは並びを崩さないため、渡した並びがそのまま残る。
    """
    by_club: dict[str, Positions] = {}
    by_club_season: dict[tuple[str, str], Positions] = {}
    if clubs is None or seasons is None or frame.empty:
        return by_club, by_club_season
    club_values = clubs.astype(str).to_numpy()
    season_values = seasons.astype(str).to_numpy()
    if order is None:
        order = np.arange(len(frame), dtype=np.intp)
    for club in np.unique(club_values):
        picked = order[club_values[order] == club]
        by_club[str(club)] = picked
        season_of = season_values[picked]
        for season in np.unique(season_of):
            by_club_season[(str(club), str(season))] = picked[season_of == season]
    return by_club, by_club_season


def _newest_first(times: Times, game_ids: NDArray[np.object_]) -> Positions:
    """`(finished_at, game_id)` の降順に並べた行位置。

    **`club_history()` の並べ替えをここへ移す。** あちらは1試合につき34回呼ばれ、
    呼び出しごとに2キーの `sort_values` を走らせていた（実測で全体の54%）。
    並びは `as_of` に依らない — 絞り込みは末尾を落とすだけで順序を変えない。

    **文字列の降順と同じ順序になる。** `finished_at` は `YYYY-MM-DDTHH:MM:SSZ` の
    固定長であり（実データ 12,540行すべて）、辞書式＝時系列である。
    """
    if times.size == 0:
        return np.empty(0, dtype=np.intp)
    ascending = np.lexsort((game_ids, times))
    return ascending[::-1].astype(np.intp, copy=True)


class EloIndex:
    """`as_of_date < 対象試合日` の最新の Elo を二分探索で引く。

    **意味は `batch/features/team_strength.py` の `elo()` と同じである。**
    あちらは呼び出しごとに `team_ratings` の全行を走査して並べ替えるため、
    12,540回の参照で1億5千万行の比較になっていた。

    **実装はここ1つだけにする。** 工程8の探索（`batch/jobs/tune_elo.py`）も
    本番の `elo()` も同じクラスを使う。2つ持つと、探索で選んだパラメータが
    本番で別の値を出すことになる（一致は `test_job_tune_elo.py` が固定する）。
    """

    def __init__(self, ratings: pd.DataFrame) -> None:
        self._dates: dict[str, list[str]] = {}
        self._elos: dict[str, list[float]] = {}
        if ratings.empty:
            return
        ordered = ratings.sort_values(["club_id", "as_of_date"], kind="stable")
        for club_id, group in ordered.groupby("club_id", sort=False):
            self._dates[str(club_id)] = [str(v) for v in group["as_of_date"]]
            self._elos[str(club_id)] = [float(v) for v in group["elo"]]

    def at(self, club_id: str, game_date: str) -> float | None:
        """`as_of_date < game_date` の最新行。無ければ None（0埋めしない）。

        **不等号は `<` である。** `team_ratings` の1行はその試合日の終了時点の
        値なので、`<=` にすると当日の結果が混入する（詳細設計 1.4）。
        """
        dates = self._dates.get(club_id)
        if dates is None:
            return None
        position = bisect.bisect_left(dates, game_date)
        if position == 0:
            return None
        return self._elos[club_id][position - 1]


class LeagueRateIndex:
    """`finished_at <= as_of` のリーグ全体の成功率を二分探索で引く（詳細設計 2.3.1）。

    **`EloIndex` と同じ形である。** 行ごとに全クラブを集計すると 2.1.1 の罠に
    戻る（`finished_team_games` は12,540行あり、146,463行ぶん走査すると18億行）。
    シーズンごとに `finished_at` の昇順で**累積の Σ成功数 / Σ試投数**を持つ。

    **`as_of` の比較の意味は変えていない。** 時刻は `_utc_ns` と同じ単位（`UNIT`）
    で持ち、引くときも同じ `<=` を使う。2.1.1 の「`as_of` の比較は1か所」は
    「同じ比較を前計算してよい」ことを禁じていない（`club_positions` も同じ）。

    **当季が0本なら前季を返す**（詳細設計 2.3.1。運営者の判断）。リーグ平均は
    1試合日で約1,000本溜まるため、効くのは開幕日だけである。
    """

    def __init__(
        self, team_stats: pd.DataFrame, game_seasons: dict[str, str],
        game_times: dict[str, int], pairs: tuple[tuple[str, str, str], ...],
    ) -> None:
        self._pairs = pairs
        # シーズン → (時刻の昇順配列, {項目: (累積成功数, 累積試投数)})
        self._by_season: dict[str, tuple[Times, dict[str, tuple[Times, Times]]]] = {}
        #: シーズン → 全期間の合計（前季へのフォールバック用）
        self._totals: dict[str, dict[str, tuple[float, float]]] = {}
        if team_stats.empty:
            return

        game_ids = team_stats["game_id"].astype(str).to_numpy()
        seasons = np.array([game_seasons.get(str(g), _NO_SEASON) for g in game_ids])
        times = np.fromiter(
            (game_times.get(str(g), NEVER) for g in game_ids),
            dtype=np.int64, count=len(game_ids))

        for season in np.unique(seasons):
            if str(season) == _NO_SEASON:
                continue
            picked = np.flatnonzero(seasons == season)
            # **終了していない試合を入れない**（`NEVER` はどの `as_of` より後）
            picked = picked[times[picked] != NEVER]
            if picked.size == 0:
                continue
            order = picked[np.argsort(times[picked], kind="stable")]
            sorted_times = times[order].astype(np.int64, copy=True)
            cumulative: dict[str, tuple[Times, Times]] = {}
            totals: dict[str, tuple[float, float]] = {}
            for name, made_column, attempt_column in pairs:
                made = pd.to_numeric(
                    team_stats[made_column].iloc[order], errors="coerce").fillna(0.0)
                attempts = pd.to_numeric(
                    team_stats[attempt_column].iloc[order], errors="coerce").fillna(0.0)
                made_sum = np.cumsum(made.to_numpy(dtype=np.float64))
                attempt_sum = np.cumsum(attempts.to_numpy(dtype=np.float64))
                cumulative[name] = (made_sum, attempt_sum)
                totals[name] = (float(made_sum[-1]), float(attempt_sum[-1]))
            self._by_season[str(season)] = (sorted_times, cumulative)
            self._totals[str(season)] = totals

    def at(
        self, season_id: str, cutoff: int, name: str,
        previous_season: str | None = None,
    ) -> float | None:
        """`finished_at <= cutoff` までのリーグ平均。無ければ前季、それも無ければ None。"""
        found = self._season_rate(season_id, cutoff, name)
        if found is not None:
            return found
        if previous_season is None:
            return None
        totals = self._totals.get(previous_season)
        if totals is None or name not in totals:
            return None
        made, attempts = totals[name]
        return None if attempts <= 0 else made / attempts

    def _season_rate(self, season_id: str, cutoff: int, name: str) -> float | None:
        entry = self._by_season.get(season_id)
        if entry is None:
            return None
        times, cumulative = entry
        if name not in cumulative:
            return None
        # **`<=` で数える。** `searchsorted(side="right")` が「cutoff 以下の件数」
        position = int(np.searchsorted(times, cutoff, side="right"))
        if position == 0:
            return None
        made_sum, attempt_sum = cumulative[name]
        attempts = float(attempt_sum[position - 1])
        return None if attempts <= 0 else float(made_sum[position - 1]) / attempts


def _is_missing(value: object) -> bool:
    """単値の欠損判定。**空文字と欠損を混ぜない。**"""
    if value is None:
        return True
    if isinstance(value, float):
        return math.isnan(value)
    return str(value) in ("nan", "NaT", "<NA>", "")


@dataclass(frozen=True)
class Prepared:
    """`as_of` に依らない前処理の結果。1つの `Dataset` に対して1回作る。"""

    team_games: pd.DataFrame
    #: `team_games` の `finished_at`（int64 ns、原順序）
    team_times: Times
    team_game_ids: NDArray[np.object_]
    player_stats: pd.DataFrame
    #: `player_game_stats` の各行の**試合の** `finished_at`（int64 ns、原順序）
    player_times: Times
    player_game_ids: NDArray[np.object_]
    #: `id` を索引にした `games`。**主キーなので重複は prepare で弾く**
    games_indexed: pd.DataFrame
    entries_by_game: dict[str, pd.DataFrame]
    #: `team_ratings` の二分探索。`elo_home` / `elo_away` が引く
    elo_index: EloIndex
    #: `team_game_stats` の本体
    team_stats: pd.DataFrame
    #: (試合ID, クラブ) → `team_game_stats` の行位置。
    #: **`as_of` の絞り込みはここに持たない** — 渡す試合は `club_history()` が
    #: 既に絞り込んだものである（`batch/features/base.py` の1経路を保つ）
    team_stats_at: dict[tuple[str, str], int]
    #: クラブ → `team_games` の行位置。**`(finished_at, game_id)` の降順**
    club_positions: dict[str, Positions] = field(default_factory=dict)
    #: (クラブ, シーズン) → 同
    club_season_positions: dict[tuple[str, str], Positions] = field(default_factory=dict)
    #: クラブ → `player_game_stats` の行位置。**原順序のまま**（`_recent_minutes` が
    #: `game_date` で並べ直し、同日の並びで結果が変わる）。所属の判定は
    #: `player_game_stats.club_id`（実績）である（特徴量の規約5）
    player_club_positions: dict[str, Positions] = field(default_factory=dict)
    #: (クラブ, シーズン) → 同。シーズンは **`team_games` 経由**で引く
    player_club_season_positions: dict[tuple[str, str], Positions] = field(
        default_factory=dict)
    #: シーズン → **直前の1季**（無ければ None）。第1段の候補集合が引く（2.3.1）。
    #: **「過去のいずれかの季」にしない** — 最初の季にはコールドスタートが残り、
    #: 昇格クラブと同じ扱いにする（2.5）
    previous_season: dict[str, str | None] = field(default_factory=dict)
    #: リーグ全体の成功率の二分探索。第3段のシュリンクの `prior` が引く（2.3.1）
    league_rates: LeagueRateIndex | None = None

    def as_of_ns(self, as_of: datetime) -> int:
        """`as_of` を `_utc_ns` と同じ単位の整数にする。**単位は `UNIT` 1か所。**"""
        return int(pd.Timestamp(as_of).tz_convert("UTC").as_unit(UNIT).value)


def prepare(dataset: Dataset) -> Prepared:
    """索引を組む。**ここでは絞り込みをしない**（`as_of` を知らない）。"""
    team_games = dataset.table("team_games")
    player_stats = dataset.table("player_game_stats")
    games = dataset.table("games")
    entries = dataset.table("game_entries")
    ratings = dataset.table("team_ratings")
    team_stats = dataset.table("team_game_stats")

    indexed = games.set_index(games["id"].astype(str), drop=False)
    if indexed.index.has_duplicates:
        # `games.id` は主キーである。重複があるのはスナップショットの欠陥で、
        # **特徴量を作る前に止める**（試合ごとに「1件に定まらない」と出すより早い）
        raise FeatureError("games の id が重複している")

    team_times = _utc_ns(team_games["finished_at"]) if len(team_games) else np.empty(0, np.int64)
    team_game_ids = team_games["game_id"].astype(object).to_numpy() if len(team_games) \
        else np.empty(0, object)

    # 選手行の「試合の終了時刻」を1回だけ引き当てる。
    # **元の実装は `game_id.isin(終了済みの試合)` だった。** 同じ集合になる —
    # どちらも「`finished_at <= as_of` かつ対象試合でない試合の行」である
    game_times = dict(zip(
        games["id"].astype(str), _utc_ns(games["finished_at"]), strict=True,
    )) if len(games) else {}
    if len(player_stats):
        player_game_ids = player_stats["game_id"].astype(object).to_numpy()
        player_times = np.fromiter(
            (game_times.get(str(gid), NEVER) for gid in player_game_ids),
            dtype=np.int64, count=len(player_game_ids),
        )
    else:
        player_game_ids = np.empty(0, object)
        player_times = np.empty(0, np.int64)

    # **チーム側は降順で持つ。** `club_history()` は新しい順に返す契約であり、
    # 並びは `as_of` に依らない（絞り込みは末尾を落とすだけ）
    club_positions, club_season_positions = _group(
        team_games,
        team_games["club_id"] if len(team_games) else None,
        team_games["season_id"] if len(team_games) else None,
        _newest_first(team_times, team_game_ids),
    )

    # 選手行のシーズンは **`team_games` 経由**で引く。`player_game_stats` に
    # シーズンの列はなく、元の実装も `finished_team_games` の当季の試合IDで
    # 絞っていた。**`games.season_id` から直に引かない** — `team_games` に
    # 無い試合は元の実装では落ちており、同じ集合にならない
    game_seasons = dict(zip(
        team_games["game_id"].astype(str), team_games["season_id"].astype(str),
        strict=True,
    )) if len(team_games) else {}
    player_club_positions, player_club_season_positions = _group(
        player_stats,
        player_stats["club_id"] if len(player_stats) else None,
        pd.Series([game_seasons.get(str(gid), _NO_SEASON) for gid in player_game_ids])
        if len(player_stats) else None,
    )

    return Prepared(
        team_games=team_games,
        team_times=team_times,
        team_game_ids=team_game_ids,
        player_stats=player_stats,
        player_times=player_times,
        player_game_ids=player_game_ids,
        games_indexed=indexed,
        elo_index=EloIndex(ratings),
        team_stats=team_stats,
        team_stats_at={
            (str(gid), str(cid)): position
            for position, (gid, cid) in enumerate(
                zip(team_stats["game_id"], team_stats["club_id"], strict=True),
            )
        } if len(team_stats) else {},
        entries_by_game={
            str(key): group for key, group in entries.groupby("game_id", sort=False)
        } if len(entries) else {},
        club_positions=club_positions,
        club_season_positions=club_season_positions,
        player_club_positions=player_club_positions,
        player_club_season_positions=player_club_season_positions,
        previous_season=_previous_seasons(dataset.tables.get("seasons")),
        # **成功率3項目の分子・分母。** `team_rate.PCT_TARGETS` と同じ対応だが、
        # `prepared` は `features` の他モジュールに依存しないため値を直書きする
        # （循環 import を作らない）。一致は `test_features_prepared.py` が固定する
        league_rates=LeagueRateIndex(
            team_stats, game_seasons,
            {k: int(v) for k, v in game_times.items()},
            (("fg2_pct", "fg2m", "fg2a"),
             ("fg3_pct", "fg3m", "fg3a"),
             ("ft_pct", "ftm", "fta")),
        ) if len(team_stats) else None,
    )


def _previous_seasons(seasons: pd.DataFrame | None) -> dict[str, str | None]:
    """シーズン → 直前の1季。

    **`seasons` が無い `Dataset` を落とさない。** 手で組んだ部分的な `Dataset`
    （`test_features_prepared.py` の Elo の検査など）は1〜2テーブルしか持たない。
    代わりに**使う側で落とす** — `Context.previous_season_id` が季を引けなければ
    `FeatureError` を投げる。ここで黙って {} を返して候補集合が静かに
    「当季だけ」に縮むことを防ぐ。

    **並べるのは `start_date` である。** `id` の降順で並べない — 文字列比較では
    `'2016-17-B1' < '2026-27-PREMIER'` のような暦順と一致しない組が出る
    （公開APIの `latestSeasonId` が同じ理由で `start_date` を使う。詳細設計 3.3）。
    """
    if seasons is None or seasons.empty:
        return {}
    ordered = seasons.sort_values("start_date", kind="stable")
    ids = [str(value) for value in ordered["id"]]
    return {
        season: (ids[position - 1] if position else None)
        for position, season in enumerate(ids)
    }
