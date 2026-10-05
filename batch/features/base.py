"""特徴量の共通の足場。

**すべての特徴量は `as_of` 時点で確定している情報だけから作る**（CLAUDE.md 絶対ルール1）。
そのための絞り込みをここに1か所で実装し、各特徴量は `Context` を通してしか
データを見ない。絞り込みが各関数に散ると、一箇所直し忘れただけでリークが復活する。

**絞り込みの経路は1つである。** 速さのために `batch/features/prepared.py` の索引を
使うが、索引は「いつ計算するか」を変えるだけで、`as_of` の比較はここにしかない。
索引を持たない `Context` を手で組んだ場合も、その場で索引を組んで同じ経路を通る
（遅いが、**2つ目の絞り込みを書かない**ことを優先する）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
from typing import TypeVar, cast

import numpy as np
import pandas as pd

from batch.features.dataset import Dataset
from batch.features.errors import FeatureError
from batch.features.prepared import Positions, Prepared, prepare

__all__ = ["Context", "FeatureError", "build_context"]

#: `cached()` が返す型。呼び出しごとに独立させる
_T = TypeVar("_T")


@dataclass(frozen=True)
class Context:
    """対象試合と、`as_of` 時点で参照してよい過去データ。"""

    game_id: str
    as_of: datetime
    game: pd.Series
    home_club_id: str
    away_club_id: str
    season_id: str
    game_date: str
    dataset: Dataset

    #: 終了済みかつ `finished_at <= as_of` の `team_games`。対象試合は除外済み
    finished_team_games: pd.DataFrame
    #: 対象試合のエントリー（試合前に公開される情報。要件 5.5）
    entries: pd.DataFrame

    #: 前処理の索引。**`as_of` の絞り込みはここではなく `build_context` が行う**
    prepared: Prepared | None = None

    @cached_property
    def index(self) -> Prepared:
        """索引。無ければその場で組む（手で組んだ `Context` のため）。"""
        return self.prepared if self.prepared is not None else prepare(self.dataset)

    @cached_property
    def _memo(self) -> dict[object, object]:
        """この試合のあいだだけ保つ記憶。`Context` は不変なので安全である。"""
        return {}

    def cached(self, key: object, factory: Callable[[], _T]) -> _T:
        """同じ引数の再計算を避ける。

        **1試合あたり `club_history()` が34回呼ばれ、引数は4通りしかない**
        （クラブ2 × 当季かどうか2）。17個の特徴量が同じ過去試合を見るためで、
        記憶しないと同じ切り出しと並べ替えを8回ずつ繰り返す（実測で全体の54%）。

        返すのは**同じオブジェクト**である。呼び出し側は結果を書き換えない
        （現在の特徴量はいずれも `.head()` や `.mean()` しか使わない）。
        """
        if key in self._memo:
            return cast("_T", self._memo[key])
        value = factory()
        self._memo[key] = value
        return value

    def club_history(self, club_id: str, *, season_only: bool) -> pd.DataFrame:
        """あるクラブの過去試合。新しい順に並べて返す。"""
        return self.cached(
            ("club_history", club_id, season_only),
            lambda: self._club_history(club_id, season_only=season_only),
        )

    def _club_history(self, club_id: str, *, season_only: bool) -> pd.DataFrame:
        index = self.index
        positions = (
            index.club_season_positions.get((club_id, self.season_id)) if season_only
            else index.club_positions.get(club_id)
        )
        if positions is None or positions.size == 0:
            return index.team_games.iloc[:0]
        cutoff = index.as_of_ns(self.as_of)
        picked = positions[
            (index.team_times[positions] <= cutoff)
            & (index.team_game_ids[positions] != self.game_id)
        ]
        # **並べ替えない。** 索引が `(finished_at, game_id)` の降順で持っている
        # （`prepared._newest_first`）。ここで並べ直すと1試合につき34回の
        # 2キーソートになる
        return index.team_games.take(picked)

    def player_history(self, club_id: str, *, season_only: bool) -> pd.DataFrame:
        """`_player_history` の記憶つきの入口。"""
        return self.cached(
            ("player_history", club_id, season_only),
            lambda: self._player_history(club_id, season_only=season_only),
        )

    def _player_history(self, club_id: str, *, season_only: bool) -> pd.DataFrame:
        """あるクラブで出場した実績のある選手行。**原順序のまま**返す。

        所属の判定は `player_game_stats.club_id`（実績）である（特徴量の規約5）。
        **並べ替えない** — `_recent_minutes` は `game_date` の降順に並べ直して
        選手ごとに先頭N件を取るため、同じ日の試合が複数あると入力の並びで
        結果が変わる。
        """
        index = self.index
        positions = (
            index.player_club_season_positions.get((club_id, self.season_id))
            if season_only else index.player_club_positions.get(club_id)
        )
        return self._take_player_rows(positions)

    def player_history_in_season(self, club_id: str, season_id: str) -> pd.DataFrame:
        """あるクラブの、**指定した季**の選手行。第1段の候補集合が前季を引く。

        **`as_of` の絞り込みは `_take_player_rows` の1か所を通る**（このクラスの
        冒頭の約束）。前季の試合はすべて `as_of` より前に終わっているはずだが、
        **比較を省かない** — 省くと「この入口だけ絞り込みを持たない」状態になり、
        リーク検証が入口ごとに当たらなくなる（詳細設計 6.1）。
        """
        return self.cached(
            ("player_history_in_season", club_id, season_id),
            lambda: self._take_player_rows(
                self.index.player_club_season_positions.get((club_id, season_id))),
        )

    def _take_player_rows(self, positions: Positions | None) -> pd.DataFrame:
        """`as_of` と対象試合の除外を当てて選手行を取り出す。**絞り込みはここだけ。**"""
        index = self.index
        if positions is None or positions.size == 0:
            return index.player_stats.iloc[:0]
        cutoff = index.as_of_ns(self.as_of)
        picked = positions[
            (index.player_times[positions] <= cutoff)
            & (index.player_game_ids[positions] != self.game_id)
        ]
        return index.player_stats.take(picked)

    @cached_property
    def previous_season_id(self) -> str | None:
        """対象試合の季の**直前の1季**。データ上の最初の季なら None。

        **季の一覧にこの試合の季が無ければ落とす。** `seasons` を持たない
        `Dataset` でも `prepare` は通るため（部分的な `Dataset` を許すため）、
        ここで気づかないと候補集合が静かに「当季だけ」に縮む。
        """
        if self.season_id not in self.index.previous_season:
            raise FeatureError(f"季の一覧にこの試合の季がない: {self.season_id}")
        return self.index.previous_season[self.season_id]

    def stats_of(self, games: pd.DataFrame, club_id: str) -> pd.DataFrame:
        """与えた試合における、あるクラブの `team_game_stats` の行。

        **`as_of` の絞り込みをここで行わない。** 渡す `games` は
        `club_history()` の戻り値であり、既に絞り込まれている。2つ目の絞り込みを
        書かないための約束である（このクラスの冒頭を参照）。

        **スタッツが無い試合は落ちる。** `possessions` が値域外で NULL の試合や、
        取り込みがスタッツまで届いていない試合が実在する（詳細設計 1.3 / 4.8）。
        行数が合わないことを欠陥として扱わず、**ある分だけで集計する**。
        """
        return self._stats_at(
            games, [club_id] * len(games) if len(games) else [])

    def stats_of_opponents(self, games: pd.DataFrame) -> pd.DataFrame:
        """同じ試合の**相手**の行。行ごとに `opponent_id` を引く。"""
        if games.empty:
            return self.index.team_stats.iloc[:0]
        return self._stats_at(games, [str(v) for v in games["opponent_id"]])

    def _stats_at(self, games: pd.DataFrame, clubs: list[str]) -> pd.DataFrame:
        index = self.index
        if games.empty or not clubs:
            return index.team_stats.iloc[:0]
        picked = [
            position
            for game_id, club in zip(games["game_id"].astype(str), clubs, strict=True)
            if (position := index.team_stats_at.get((game_id, club))) is not None
        ]
        if not picked:
            return index.team_stats.iloc[:0]
        return index.team_stats.take(np.asarray(picked, dtype=np.intp))

    @cached_property
    def finished_player_stats(self) -> pd.DataFrame:
        """同条件の `player_game_stats`（クラブで絞らない）。

        **使うときだけ作る。** 146,463行の実データを試合ごとに切り出すと、
        6,270試合で37億行の複製になる（2026-10-02 の実測）。本番の特徴量は
        `player_history()` を通るためここには来ない。
        """
        index = self.index
        if index.player_stats.empty:
            return index.player_stats
        cutoff = index.as_of_ns(self.as_of)
        keep = (index.player_times <= cutoff) & (index.player_game_ids != self.game_id)
        rows: pd.DataFrame = index.player_stats.loc[keep]
        return rows


def build_context(
    game_id: str, as_of: datetime, dataset: Dataset,
    prepared: Prepared | None = None,
) -> Context:
    """`as_of` による絞り込みを1か所で行う。

    - 絞り込みは **`finished_at <= as_of`**。`tipoff_at` ではない（17:05開始・19:05終了の
      試合が19:05開始の試合の確定情報として混入する）
    - `finished_at` が NULL の試合（`SCHEDULED` / `POSTPONED` / `CANCELLED`）は入れない
    - **対象試合自身を明示的に除外する。** `as_of` の比較だけに頼らない
    """
    if as_of.tzinfo is None:
        raise FeatureError("as_of にタイムゾーンが必要")

    index = prepared if prepared is not None else prepare(dataset)

    try:
        row = index.games_indexed.loc[game_id]
    except KeyError:
        raise FeatureError("対象試合が1件に定まらない") from None
    if isinstance(row, pd.DataFrame):
        # `prepare` が重複を弾いているので起こらないが、**関門は残す**
        raise FeatureError("対象試合が1件に定まらない")
    game = row

    # **絞り込みの意味は以前と同じ。** 変えたのは「いつ計算するか」だけである。
    # `finished_at` は索引を組むときに一度だけ数値へ直してあり、NULL は
    # どの `as_of` よりも後になる値を持つため、比較だけで除外される。
    # **原順序を保つ**（`_recent_minutes` は同日の並びで結果が変わる）。
    cutoff = index.as_of_ns(as_of)
    keep_team = (index.team_times <= cutoff) & (index.team_game_ids != game_id)
    finished = index.team_games.loc[keep_team]

    entries = index.entries_by_game.get(game_id)
    if entries is None:
        entries = dataset.table("game_entries").iloc[:0]

    return Context(
        game_id=game_id,
        as_of=as_of,
        game=game,
        home_club_id=str(game["home_club_id"]),
        away_club_id=str(game["away_club_id"]),
        season_id=str(game["season_id"]),
        game_date=str(game["game_date"]),
        dataset=dataset,
        finished_team_games=finished,
        entries=entries,
        prepared=index,
    )
