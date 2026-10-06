"""実績の集計（詳細設計 4.14。4.2 のステップ6b）。

    python -m batch.jobs.summarize_stats [--dry-run]

戦績・スタッツの閲覧（要件 F-10 / F-15）が読む2表を作る。

**実行時に集計しない。** 1選手の通算を `player_game_stats` から引くと1ページ約
1,200行を読み、D1 の読取枠（500万行/日）は1日4,000ページ閲覧で尽きる
（基本設計 3.2）。畳んでおけば1ページ2クエリ・数十行になる。

**洗い替えず upsert だけで更新する。** 約4,650行は1リクエスト（160行）に収まらず、
分割すると後のリクエストの DELETE が前のリクエストで入れた行を消す。upsert で
足りる根拠は、取り込みが試合を `id` で upsert して**削除しない**ことである。

**入力はスナップショットだけ**（絶対ルール3）。D1 を読まない。
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from batch.features.dataset import load_snapshot
from batch.loader.api import InternalApi, LoaderError
from batch.loader.limits import max_rows_per_request

DEFAULT_SNAPSHOT = Path("batch/snapshot")

#: `/players/[id]` の静的生成の出典（詳細設計 4.14 / 5.6）。**ビルド時にだけ読む**
DEFAULT_PLAYER_INDEX = Path("web/data/players.csv")

#: シーズンの CSV（静的生成の範囲を決める。`web/lib/seasons.ts` と同じ出典）
DEFAULT_SEASONS_CSV = Path("db/seeds/master/seasons.csv")

#: 静的生成するシーズン数（要件 8.2。`STATIC_SEASONS` と同じ値）
STATIC_SEASONS = 3

PLAYER_PER_REQUEST = max_rows_per_request("player_stat_summary")
TEAM_PER_REQUEST = max_rows_per_request("team_stat_summary")

#: 通算の行の `scope_key` と `club_id`。**NULL にしない**（詳細設計 1.9）
ACROSS = ""

#: ボックススコアのカウント14項目。**`plus_minus` を含めない**（1.9）
COUNTS = (
    "fg2m", "fg2a", "fg3m", "fg3a", "ftm", "fta",
    "oreb", "dreb", "ast", "tov", "stl", "blk", "pf", "fd",
)


class SummarizeError(RuntimeError):
    """集計できない入力。黙って既定値を入れない。"""


@dataclass(frozen=True)
class PlayerStat:
    """`player_stat_summary` の1行。保存するのは合計で、平均は API が導出する。"""

    player_id: str
    scope: str
    scope_key: str
    club_id: str
    games: int
    games_started: int
    minutes: float
    counts: dict[str, int]
    pts: int


@dataclass(frozen=True)
class TeamStat:
    """`team_stat_summary` の1行。**母数を2つ持つ**（`games` / `stat_games`）。"""

    club_id: str
    scope: str
    scope_key: str
    games: int
    wins: int
    points_for: int
    points_against: int
    stat_games: int
    counts: dict[str, int]


@dataclass
class Outcome:
    players: list[PlayerStat] = field(default_factory=list)
    teams: list[TeamStat] = field(default_factory=list)
    #: 静的生成する選手（直近3シーズンに出場）
    recent_players: list[tuple[str, str]] = field(default_factory=list)
    finished_games: int = 0


# --- 入力の整え ---


def _finished(games: pd.DataFrame) -> pd.DataFrame:
    """終了した試合だけ（詳細設計 1.9.1）。`game_id` → `season_id` の対応を作る。

    **季の判定に `games.season_id` を使う。** 2.1.1 の注記4は特徴量について
    `team_games` 経由を定めているが、あれは索引ありと索引なしで**特徴量の集合を
    完全に一致させる**ための規約である。ここは表示のための集計であり、
    取り込んだ試合すべてを数えたい。
    """
    for column in ("id", "season_id", "status"):
        if column not in games.columns:
            raise SummarizeError(f"games に必要な列がない: {column}")
    done = games[games["status"].astype(str) == "FINISHED"]
    return done[["id", "season_id", "home_score", "away_score"]].copy()


def _ints(frame: pd.DataFrame, columns: Sequence[str]) -> dict[str, int]:
    """合計を整数で取る。**欠損は0として数えない — 列ごとに dropna する。**

    値が入っている試合だけを足す。`plus_minus` のように「キーが無い年度がある」
    列は集計しない（1.9）ため、ここで足す14項目は欠損がほぼ無い。
    """
    out: dict[str, int] = {}
    for column in columns:
        if column not in frame.columns:
            raise SummarizeError(f"必要な列がない: {column}")
        out[column] = int(frame[column].fillna(0).sum())
    return out


# --- 選手 ---


def summarize_players(stats: pd.DataFrame, finished: pd.DataFrame) -> list[PlayerStat]:
    """選手を `(player_id, season_id, club_id)` と `(player_id)` で畳む。

    **クラブは `player_game_stats.club_id`（実績）で判定する**（2.1 の規約6）。
    `players` の現在の所属や `player_seasons` の断面を使うと、移籍した選手の
    過去の記録が新クラブへ移る。
    """
    joined = stats.merge(
        finished[["id", "season_id"]], left_on="game_id", right_on="id", how="inner")
    if joined.empty:
        return []

    rows: list[PlayerStat] = []

    def row(frame: pd.DataFrame, scope: str, scope_key: str, club_id: str) -> PlayerStat:
        started = frame["started"].fillna(0).astype(float).sum() if "started" in frame else 0.0
        return PlayerStat(
            player_id=str(frame["player_id"].iloc[0]),
            scope=scope, scope_key=scope_key, club_id=club_id,
            games=len(frame),
            games_started=int(started),
            minutes=round(float(frame["minutes"].fillna(0.0).sum()), 1),
            counts=_ints(frame, COUNTS),
            pts=int(frame["pts"].fillna(0).sum()),
        )

    for (player_id, season_id, club_id), frame in joined.groupby(
        ["player_id", "season_id", "club_id"], sort=True,
    ):
        rows.append(row(frame, "SEASON", str(season_id), str(club_id)))
        del player_id

    for _player_id, frame in joined.groupby("player_id", sort=True):
        rows.append(row(frame, "CAREER", ACROSS, ACROSS))

    return rows


# --- クラブ ---


def summarize_teams(team_games: pd.DataFrame, team_stats: pd.DataFrame,
                    finished: pd.DataFrame) -> list[TeamStat]:
    """クラブを `(club_id, season_id)` と `(club_id)` で畳む。

    **母数を2つ持つ。** `games` は `team_games` で `result` が入っている試合数、
    `stat_games` は `team_game_stats` に行がある試合数である。取り込みが
    `games` と `stats` を続けて投げ、その間で失敗すると試合行だけが残るため、
    **1つに畳むと母数が嘘になる**（詳細設計 1.9）。
    """
    done_ids = set(finished["id"].astype(str))
    played = team_games[
        team_games["game_id"].astype(str).isin(done_ids)
        & team_games["result"].notna()
    ].copy()
    if played.empty:
        return []

    # 得点は `games` から取る（`team_game_stats.pts` を使うと stat_games の母数に縛られる）
    scores = finished.set_index(finished["id"].astype(str))
    played["_gid"] = played["game_id"].astype(str)
    home = played["is_home"].astype(int) == 1
    played["points_for"] = [
        _score(scores, gid, own=True, is_home=flag)
        for gid, flag in zip(played["_gid"], home, strict=True)
    ]
    played["points_against"] = [
        _score(scores, gid, own=False, is_home=flag)
        for gid, flag in zip(played["_gid"], home, strict=True)
    ]

    boxes = team_stats[team_stats["game_id"].astype(str).isin(done_ids)].copy()
    boxes["_gid"] = boxes["game_id"].astype(str)
    season_of = dict(zip(scores.index, scores["season_id"].astype(str), strict=True))
    boxes["season_id"] = boxes["_gid"].map(season_of)

    rows: list[TeamStat] = []

    def row(games: pd.DataFrame, box: pd.DataFrame, scope: str, scope_key: str) -> TeamStat:
        return TeamStat(
            club_id=str(games["club_id"].iloc[0]),
            scope=scope, scope_key=scope_key,
            games=len(games),
            wins=int(games["result"].fillna(0).astype(float).sum()),
            points_for=int(games["points_for"].fillna(0).sum()),
            points_against=int(games["points_against"].fillna(0).sum()),
            stat_games=len(box),
            counts=_ints(box, COUNTS) if not box.empty else dict.fromkeys(COUNTS, 0),
        )

    for (club_id, season_id), frame in played.groupby(["club_id", "season_id"], sort=True):
        box = boxes[
            (boxes["club_id"].astype(str) == str(club_id))
            & (boxes["season_id"] == str(season_id))
        ]
        rows.append(row(frame, box, "SEASON", str(season_id)))

    for club_id, frame in played.groupby("club_id", sort=True):
        box = boxes[boxes["club_id"].astype(str) == str(club_id)]
        rows.append(row(frame, box, "CAREER", ACROSS))

    return rows


def _score(scores: pd.DataFrame, game_id: str, *, own: bool, is_home: bool) -> float:
    """その試合のそのクラブ（または相手）の得点。欠損は 0 として扱わず NaN を返す。"""
    if game_id not in scores.index:
        return float("nan")
    wants_home = is_home if own else not is_home
    value = scores.at[game_id, "home_score" if wants_home else "away_score"]
    if pd.isna(value):
        return float("nan")
    return float(str(value))


# --- 静的生成する選手の一覧 ---


def recent_season_ids(seasons_csv: Path, limit: int = STATIC_SEASONS) -> list[str]:
    """直近Nシーズンの `season_id`。

    **`start_date` の降順で取る。`id` の降順で並べない** — 文字列比較では将来の
    採番で崩れる（`web/lib/seasons.ts` / 詳細設計 3.3 の `latestSeasonId` と同じ理由）。
    """
    if not seasons_csv.exists():
        raise SummarizeError(f"シーズンの CSV がない: {seasons_csv}")
    frame = pd.read_csv(seasons_csv, dtype=str)
    for column in ("id", "start_date"):
        if column not in frame.columns:
            raise SummarizeError(f"seasons.csv に {column} 列がない")
    ordered = frame.sort_values("start_date", ascending=False)
    return [str(x) for x in ordered["id"].head(limit)]


def recent_players(stats: pd.DataFrame, finished: pd.DataFrame,
                   players: pd.DataFrame, season_ids: Sequence[str]) -> list[tuple[str, str]]:
    """直近3シーズンに1試合でも出場した選手（要件 8.2）。

    並びは `player_id` の昇順。**実行ごとに同じ順序にする** — 差分が無意味に動かない。
    """
    wanted = set(season_ids)
    recent_games = set(
        finished[finished["season_id"].astype(str).isin(wanted)]["id"].astype(str))
    appeared = sorted(
        {str(x) for x in stats[stats["game_id"].astype(str).isin(recent_games)]["player_id"]})
    names = dict(zip(players["id"].astype(str), players["name"].astype(str), strict=True))
    return [(pid, names.get(pid, "")) for pid in appeared]


def write_player_index(path: Path, rows: Sequence[tuple[str, str]]) -> None:
    """`web/data/players.csv` を書く。**生成物であり手で編集しない**（CLAUDE.md）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["player_id,name"]
    for player_id, name in rows:
        if "," in name or '"' in name:
            raise SummarizeError(f"氏名に区切り文字が含まれる: {player_id}")
        lines.append(f"{player_id},{name}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --- 送信 ---


def _player_payload(rows: Sequence[PlayerStat]) -> list[dict[str, object]]:
    return [
        {
            "playerId": r.player_id, "scope": r.scope, "scopeKey": r.scope_key,
            "clubId": r.club_id, "games": r.games, "gamesStarted": r.games_started,
            "minutes": r.minutes, "pts": r.pts, **r.counts,
        }
        for r in rows
    ]


def _team_payload(rows: Sequence[TeamStat]) -> list[dict[str, object]]:
    return [
        {
            "clubId": r.club_id, "scope": r.scope, "scopeKey": r.scope_key,
            "games": r.games, "wins": r.wins,
            "pointsFor": r.points_for, "pointsAgainst": r.points_against,
            "statGames": r.stat_games, **r.counts,
        }
        for r in rows
    ]


def send(api: InternalApi, outcome: Outcome) -> None:
    """**行数上限はテーブルごとに守る**（3.4）。洗い替えないため順序に意味はない。"""
    for start in range(0, len(outcome.players), PLAYER_PER_REQUEST):
        api.post("stat-summary", {
            "playerStats": _player_payload(
                outcome.players[start:start + PLAYER_PER_REQUEST]),
        })
    for start in range(0, len(outcome.teams), TEAM_PER_REQUEST):
        api.post("stat-summary", {
            "teamStats": _team_payload(outcome.teams[start:start + TEAM_PER_REQUEST]),
        })


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _log(api: InternalApi, status: str, rows: int) -> None:
    try:
        api.post("log", {
            "id": f"summarize-{uuid.uuid4().hex[:8]}",
            "job": "summarize_stats",
            "startedAt": _iso_now(), "finishedAt": _iso_now(),
            "status": status, "rowsAffected": rows,
        })
    except LoaderError:
        print("  - ログの記録に失敗した")


def build(snapshot_dir: Path = DEFAULT_SNAPSHOT,
          seasons_csv: Path = DEFAULT_SEASONS_CSV) -> Outcome:
    """集計だけを行う（送信しない）。テストと `--dry-run` が使う。"""
    dataset = load_snapshot(snapshot_dir)
    finished = _finished(dataset.table("games"))
    stats = dataset.table("player_game_stats")
    return Outcome(
        players=summarize_players(stats, finished),
        teams=summarize_teams(
            dataset.table("team_games"), dataset.table("team_game_stats"), finished),
        recent_players=recent_players(
            stats, finished, dataset.table("players"),
            recent_season_ids(seasons_csv)),
        finished_games=len(finished),
    )


def run(*, api: InternalApi, snapshot_dir: Path = DEFAULT_SNAPSHOT,
        seasons_csv: Path = DEFAULT_SEASONS_CSV,
        player_index: Path = DEFAULT_PLAYER_INDEX) -> Outcome:
    outcome = build(snapshot_dir, seasons_csv)
    if outcome.players or outcome.teams:
        send(api, outcome)
    write_player_index(player_index, outcome.recent_players)
    return outcome


def _report(outcome: Outcome) -> None:
    seasons = sum(1 for r in outcome.players if r.scope == "SEASON")
    careers = sum(1 for r in outcome.players if r.scope == "CAREER")
    print(f"集計: 終了した試合 {outcome.finished_games}件")
    print(f"  選手 {len(outcome.players)}行（季 {seasons} / 通算 {careers}）")
    print(f"  クラブ {len(outcome.teams)}行")
    print(f"  静的生成する選手 {len(outcome.recent_players)}人")
    # **母数が2つあることを出す。** 1つに畳むと嘘になる（1.9）
    gap = [t for t in outcome.teams if t.stat_games < t.games]
    if gap:
        print(f"  スタッツが欠ける試合があるクラブ×スコープ: {len(gap)}件"
              "（ボックススコアの母数は stat_games）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="実績を集計して D1 へ送る")
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--seasons", type=Path, default=DEFAULT_SEASONS_CSV)
    parser.add_argument("--player-index", type=Path, default=DEFAULT_PLAYER_INDEX)
    parser.add_argument("--dry-run", action="store_true", help="D1 には書かない")
    args = parser.parse_args(argv)

    try:
        api = InternalApi(
            os.environ.get("API_BASE_URL", ""),
            os.environ.get("INGEST_TOKEN", ""),
            dry_run=args.dry_run,
        )
        outcome = run(api=api, snapshot_dir=args.snapshot, seasons_csv=args.seasons,
                      player_index=args.player_index)
    except SummarizeError as error:
        print(f"中止: {error}", file=sys.stderr)
        return 1
    except LoaderError as error:
        print(f"中止: 内部APIとの通信に失敗した: {error}", file=sys.stderr)
        return 1
    _report(outcome)
    if not args.dry_run and (outcome.players or outcome.teams):
        _log(api, "SUCCESS", len(outcome.players) + len(outcome.teams))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
