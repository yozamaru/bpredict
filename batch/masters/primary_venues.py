"""本拠会場の導出（詳細設計 1.2 / 4.11）。

**入力はスナップショットの `games` と、手入力の CSV だけ。** D1 を入力として
読まない（CLAUDE.md 絶対ルール3）。

| 区分 | 出典 |
|---|---|
| 過去シーズン | 取り込み済みの試合から**1回だけ**導出して CSV に固定する |
| 当季（まだ試合がない） | **手入力。** 行ごとに出典URLを記録する |

**事前の事実を事後のデータから復元している。** 本拠アリーナはシーズン開幕前に
公表される事実なので特徴量として使うこと自体はリークではないが、**こちらにその
公表を取る経路がない**。運営者が承知のうえで許容した（2026-10-03）。詳細設計 1.2。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

#: 手入力と導出をまとめて置く CSV。`source` が空なら導出、URL なら手入力
PRIMARY_VENUE_CSV = Path("db/seeds/master/club_primary_venues.csv")

CSV_COLUMNS = ("season_id", "club_id", "venue_id", "games", "share", "source")


class PrimaryVenueError(RuntimeError):
    """導出できない入力。黙って既定値を返さない。"""


@dataclass(frozen=True)
class PrimaryVenue:
    """クラブ・シーズンごとの本拠会場（詳細設計 1.2 / 4.11）。

    **過去シーズンは取り込み済みの試合から1回だけ導出し、CSV に固定する。**
    当季は手入力である（まだ試合が無い）。

    **承知しておく — これは事前の事実を事後のデータから復元している。** 本拠は
    開幕前に公表される事実だが、その公表を取る経路がこちらに無い。
    **運営者が承知のうえで許容した判断である。**

    `share`（占有率）を残すのは**判断の材料を捨てないため**で、実測の最小は
    0.367 である（その組で「最も多く使った会場」を本拠と呼べるかは疑わしい）。
    **閾値は設けない** — 設計文書に根拠のない定数を増やさない。
    """

    season_id: str
    club_id: str
    venue_id: str
    #: 最頻会場でのホーム試合数
    games: int
    #: 占有率（最頻会場の試合数 ÷ そのクラブ・そのシーズンのホーム試合数）
    share: float
    #: 手入力なら出典URL、導出なら空文字
    source: str = ""

    @property
    def is_manual(self) -> bool:
        """手入力の行か（`source` に出典URLがあるか）。

        **手入力の行を導出で置き換えない** — 開幕前に調べて入れた値が
        シーズン途中で勝手に変わる（詳細設計 4.11）。
        """
        return bool(self.source)


@dataclass
class BuildResult:
    """導出の結果。**止めて報告するものと、報告だけするものを分ける。**

    `conflicts`（手入力と導出の食い違い）と `ties`（同数）は**報告のみでジョブは
    継続する** — どちらかが誤りであり、**黙って上書きすると過去の本拠が遡って
    変わる**（詳細設計 4.9 の会場名と同じ扱い）。

    `ties` は実データでは1件も起きない（225組）。**それでも数える** — 決まって
    いない規則を実装に残すと、将来データが変わったときに実行ごとに値が変わる。
    """

    rows: list[PrimaryVenue] = field(default_factory=list)
    #: ホーム試合が1件もない、または `venue_id` が全件 NULL だった組
    skipped: list[tuple[str, str]] = field(default_factory=list)
    #: 手入力の行と導出が食い違った組（報告のみ。ジョブは継続）
    conflicts: list[tuple[str, str, str, str]] = field(default_factory=list)
    #: 同数が起きた組（実データでは起きないが、起きたら知らせる）
    ties: list[tuple[str, str]] = field(default_factory=list)


def _home_rows(games: pd.DataFrame) -> pd.DataFrame:
    """終了済み・`venue_id` あり・ホーム側の試合。

    **未実施の試合を含めない**（特徴量の規約4）。日程が公表済みであっても、
    そこから数えるのは「未来の試合を見る」ことになる（詳細設計 1.2）。
    """
    needed = ("status", "venue_id", "season_id", "home_club_id", "game_date")
    missing = [c for c in needed if c not in games.columns]
    if missing:
        raise PrimaryVenueError(f"games に必要な列がない: {missing}")
    finished = games[(games["status"] == "FINISHED") & games["venue_id"].notna()]
    out = finished[["season_id", "home_club_id", "venue_id", "game_date"]].rename(
        columns={"home_club_id": "club_id"})
    return out.astype({"season_id": str, "club_id": str, "venue_id": str})


def derive(games: pd.DataFrame) -> BuildResult:
    """`(season_id, club_id)` ごとに最頻のホーム会場を取る（詳細設計 1.2）。

    **同数のときは、より早く使った会場を採る**（同じ日なら `venue_id` の昇順）。
    実データでは225組すべてで同数が起きないが、**決めていない規則を実装に残さない**
    （残すとデータが変わったときに実行ごとに値が変わる）。

    **占有率の下限を設けない。** 閾値は設計文書に根拠のない定数になる。
    """
    rows = _home_rows(games)
    result = BuildResult()
    if rows.empty:
        return result

    for (season_id, club_id), group in rows.groupby(["season_id", "club_id"], sort=True):
        counts = group.groupby("venue_id").agg(
            games=("game_date", "size"), first=("game_date", "min"))
        best = int(counts["games"].max())
        tied = counts[counts["games"] == best]
        if len(tied) > 1:
            result.ties.append((str(season_id), str(club_id)))
        # 同数は「早く使った方」→ 同日なら venue_id 昇順
        tied = tied.sort_index().sort_values("first", kind="stable")
        venue_id = str(tied.index[0])
        result.rows.append(PrimaryVenue(
            season_id=str(season_id), club_id=str(club_id), venue_id=venue_id,
            games=best, share=best / len(group)))
    return result


def load_csv(path: Path = PRIMARY_VENUE_CSV) -> list[PrimaryVenue]:
    """CSV を読む。無ければ空。**列が足りなければ落とす**（黙って補わない）。"""
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or tuple(reader.fieldnames) != CSV_COLUMNS:
            raise PrimaryVenueError(
                f"{path} の列が {CSV_COLUMNS} と違う: {reader.fieldnames}")
        out: list[PrimaryVenue] = []
        for line in reader:
            out.append(PrimaryVenue(
                season_id=line["season_id"].strip(),
                club_id=line["club_id"].strip(),
                venue_id=line["venue_id"].strip(),
                games=int(line["games"] or 0),
                share=float(line["share"] or 0.0),
                source=line["source"].strip()))
    return out


def merge(derived: BuildResult, existing: list[PrimaryVenue]) -> BuildResult:
    """手入力の行を導出で置き換えない（詳細設計 4.11）。

    **手入力の行を導出が消すと、開幕前に調べて入れた値がシーズン途中で勝手に
    変わる。** 食い違いは `conflicts` に積んで報告するだけで、ジョブは継続する。
    """
    manual = {(r.season_id, r.club_id): r for r in existing if r.is_manual}
    out = BuildResult(skipped=list(derived.skipped), ties=list(derived.ties))
    seen: set[tuple[str, str]] = set()
    for row in derived.rows:
        key = (row.season_id, row.club_id)
        seen.add(key)
        kept = manual.get(key)
        if kept is None:
            out.rows.append(row)
            continue
        if kept.venue_id != row.venue_id:
            out.conflicts.append(
                (row.season_id, row.club_id, kept.venue_id, row.venue_id))
        out.rows.append(kept)
    for key, row in sorted(manual.items()):
        if key not in seen:
            out.rows.append(row)
    out.rows.sort(key=lambda r: (r.season_id, r.club_id))
    return out


def write_csv(rows: list[PrimaryVenue], path: Path = PRIMARY_VENUE_CSV) -> None:
    """CSV に書き出す。**占有率と試合数を残す**（判断の材料を捨てない。4.11）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        for row in rows:
            writer.writerow([
                row.season_id, row.club_id, row.venue_id,
                row.games, f"{row.share:.4f}", row.source])


def primary_of(rows: list[PrimaryVenue]) -> dict[tuple[str, str], str]:
    """`(season_id, club_id) -> venue_id` の対応表。"""
    return {(r.season_id, r.club_id): r.venue_id for r in rows}


def is_primary_venue(
    games: pd.DataFrame, primary: dict[tuple[str, str], str],
) -> pd.Series:
    """`games.is_primary_venue` の値（詳細設計 4.12）。

    **`primary_venue_id` が分からないクラブ×シーズンは 1 のまま**にする（DDL の
    `DEFAULT 1`）。「本拠が分からない」と「代替会場である」は違う。
    """
    keys = list(zip(games["season_id"].astype(str),
                    games["home_club_id"].astype(str), strict=True))
    venues = games["venue_id"]
    values = []
    for key, venue in zip(keys, venues, strict=True):
        expected = primary.get(key)
        if expected is None or pd.isna(venue):
            values.append(1)
        else:
            values.append(1 if str(venue) == expected else 0)
    return pd.Series(values, index=games.index, dtype="int64")
