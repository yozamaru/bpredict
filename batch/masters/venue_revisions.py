"""会場の名称・収容人数の履歴の構築（詳細設計 4.9）。

**入力はスナップショットの `games` と、手入力の CSV 1本。** D1 も HTTP も知らない
（CLAUDE.md 絶対ルール3）。`team_ratings`（Elo）と同じく**全期間を再計算して洗い替える**
ため、取り込み順序に依存せず、何度実行しても同じ結果になる。

名称の出典は `games.venue_name_at_game`（その試合時点の `StadiumNameJ`）だけである。
`venues.name` は初出の名称で固定されるため（過去試合の会場表示が遡って変わらないように
するため）、試合行に残した値以外に履歴を作る手段がない（詳細設計 1.2）。
"""

from __future__ import annotations

import csv
import itertools
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

#: 最後の区間の終わり。DDL の `valid_to` の既定値と同じ
OPEN_ENDED = "9999-12-31"

#: 手入力の収容人数（詳細設計 1.2）。行ごとに出典URLを持つ
CAPACITY_CSV = Path("db/seeds/master/venue_revisions.csv")
CAPACITY_COLUMNS = ("venue_id", "valid_from", "capacity", "source")


class VenueRevisionError(RuntimeError):
    """入力が矛盾している。**自動で解決せず中止する**（詳細設計 4.9）。"""


@dataclass(frozen=True)
class Revision:
    venue_id: str
    valid_from: str
    valid_to: str
    name: str
    capacity: int | None = None


@dataclass
class BuildResult:
    revisions: list[Revision] = field(default_factory=list)
    #: 名称が1つも取れず、`venues.name` へのフォールバックになる会場
    fallback_venue_ids: list[str] = field(default_factory=list)
    #: 同一シーズン内で名称が変わった会場（表記ゆれの疑い。報告するが止めない）
    intra_season_changes: list[str] = field(default_factory=list)
    #: 収容人数が当たらなかった CSV の行
    unmatched_capacity: list[str] = field(default_factory=list)


def _previous_day(value: str) -> str:
    return (date.fromisoformat(value) - timedelta(days=1)).isoformat()


def _name_periods(rows: pd.DataFrame) -> list[tuple[str, str, str]]:
    """1会場分の (valid_from, valid_to, name) を作る。

    `game_date` 昇順に見て、名称が前と変わった時点で新しい区間を開く。直前の区間の
    `valid_to` はその前日、最後の区間は `9999-12-31`。
    """
    periods: list[tuple[str, str, str]] = []
    for row in rows.itertuples(index=False):
        name, game_date = str(row.venue_name_at_game), str(row.game_date)
        if periods and periods[-1][2] == name:
            continue
        if periods:
            start, _, previous_name = periods[-1]
            if game_date <= start:
                raise VenueRevisionError(
                    f"名称の変更日が直前の区間の開始日以前にある: {game_date}"
                )
            periods[-1] = (start, _previous_day(game_date), previous_name)
        periods.append((game_date, OPEN_ENDED, name))
    return periods


def _season_of(games: pd.DataFrame) -> dict[str, str]:
    """`game_date` → `season_id`。同一シーズン内の名称変更を検出するために使う。"""
    return {str(row.game_date): str(row.season_id) for row in games.itertuples(index=False)}


def build_name_periods(games: pd.DataFrame) -> BuildResult:
    """`games` から会場ごとの名称区間を作る。

    **`venue_name_at_game` が NULL の試合は使わない。** 欠損を「名称が変わった」と
    読まない（0 と欠損を区別するのと同じ理由。詳細設計 4.4 の正規化3）。会場の全試合が
    NULL なら区間を作らず、表示は `venues.name` へフォールバックする（詳細設計 1.2）。
    """
    for column in ("venue_id", "game_date", "venue_name_at_game", "season_id"):
        if column not in games.columns:
            raise VenueRevisionError(f"games に {column} がない")

    result = BuildResult()
    with_venue = games[games["venue_id"].notna()]
    seasons = _season_of(with_venue)

    for venue_id, rows in with_venue.groupby("venue_id", sort=True):
        named = rows[rows["venue_name_at_game"].notna()]
        named = named[named["venue_name_at_game"].astype(str).str.strip() != ""]
        if named.empty:
            result.fallback_venue_ids.append(str(venue_id))
            continue
        ordered = named.sort_values(["game_date", "venue_id"])
        periods = _name_periods(ordered)
        for start, end, name in periods:
            result.revisions.append(Revision(str(venue_id), start, end, name))
        # 2区間目以降の開始日が、直前の区間の開始日と同じシーズンなら表記ゆれの疑い
        for (previous_start, _, _), (start, _, _) in itertools.pairwise(periods):
            if seasons.get(previous_start) == seasons.get(start):
                result.intra_season_changes.append(
                    f"{venue_id}: {previous_start} → {start}（同一シーズン内）"
                )
    return result


def load_capacities(path: Path = CAPACITY_CSV) -> dict[tuple[str, str], int | None]:
    """手入力の収容人数を読む。ファイルがなければ空（全会場 NULL）。"""
    if not path.exists():
        return {}
    out: dict[tuple[str, str], int | None] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = set(CAPACITY_COLUMNS) - set(reader.fieldnames or ())
        if missing:
            raise VenueRevisionError(f"{path.name} に列がない: {sorted(missing)}")
        for row in reader:
            key = (row["venue_id"].strip(), row["valid_from"].strip())
            if key in out:
                raise VenueRevisionError(f"{path.name} に重複した行がある: {key}")
            raw = row["capacity"].strip()
            out[key] = int(raw) if raw else None
    return out


def build(
    games: pd.DataFrame,
    capacities: dict[tuple[str, str], int | None] | None = None,
) -> BuildResult:
    """名称区間に収容人数を突き合わせる。

    **CSV の `(venue_id, valid_from)` が名称区間のどれにも一致しなければ中止する**
    （詳細設計 4.9）。収容人数だけのために名称区間を勝手に分割しない。CSV の
    `valid_from` を区間の開始日に合わせるのは運営者の判断である。
    """
    result = build_name_periods(games)
    table = load_capacities() if capacities is None else capacities
    if not table:
        return result

    keys = {(r.venue_id, r.valid_from) for r in result.revisions}
    unmatched = sorted(f"{venue}/{start}" for venue, start in table if (venue, start) not in keys)
    if unmatched:
        raise VenueRevisionError(
            f"収容人数の行が名称区間に一致しない（{len(unmatched)}件）: {unmatched[:5]}"
        )
    result.revisions = [
        Revision(r.venue_id, r.valid_from, r.valid_to, r.name,
                 table.get((r.venue_id, r.valid_from)))
        for r in result.revisions
    ]
    return result
