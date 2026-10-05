"""登録選手一覧を取り込む（詳細設計 1.2 / 4.13）。

    python -m batch.jobs.ingest_rosters [--season 2026-27-PREMIER] [--dry-run]

**1回だけ行う。** 登録選手は季ごとに確定するため、日次ジョブには入れない。

**クラブ×シーズンの組はスナップショットの `club_seasons` から取る**（238組）。
`select[club]` を読むために `/roster/?year=<YYYY>` を11回取る案もあるが、
**同じ情報を既に持っている** — 11リクエスト節約できる。

**出せないものは送らない。** `height_cm` / `roster_type` / `joined_on` /
`left_on` はいずれも出典が無い（要件 5.3）。口の側に「NULL で上書きしない」
保護が掛かっているため、将来出典が見つかっても先に消すことはない。
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from batch.features.dataset import load_snapshot, write_snapshot
from batch.loader.api import InternalApi, LoaderError, Poster
from batch.loader.payload import rosters_payload, snapshot_rows
from batch.parser.errors import ParseError, ParseFailureTracker, ValidationError
from batch.parser.roster_parser import RosterEntry, parse_roster
from batch.parser.terms import report_terms_change
from batch.scraper.client import (
    RateLimitedClient,
    ScraperError,
    ScrapingStopped,
    TermsReporter,
)
from batch.scraper.roster import roster_url

DEFAULT_SNAPSHOT = Path("batch/snapshot")
DEFAULT_STATE_PATH = Path("batch/.scraper-state/state.json")

#: スナップショットへ写すテーブル（部分書き出し。基本設計 2.2）。
SNAPSHOT_TABLES = ("players", "player_seasons")


class Fetcher(Protocol):
    """取得する側が要求する最小の口。

    **`RateLimitedClient` そのものを要求しない**（`Poster` と同じ理由）。
    テストが HTTP の作法と状態ファイルを持つ本物を組む必要がなくなる。
    **何を呼ぶのかは型で残す** — 関門（`verify_policy`）を省いた実装を
    黙って渡せないようにするためである。
    """

    def verify_policy(self, terms_reporter: TermsReporter | None = ...) -> None: ...

    def get(self, url: str) -> str: ...


class RosterJobError(RuntimeError):
    """ジョブを続けられない。**例外に本文を入れない**（絶対ルール4）。"""


@dataclass
class Pair:
    """取りに行く組。"""

    season_id: str
    club_id: str
    year: int


@dataclass
class Result:
    status: str = "SUCCESS"
    pairs: int = 0
    fetched: int = 0
    players: int = 0
    seasons: int = 0
    dropped: int = 0
    unregistered: int = 0
    empty: int = 0
    failed: int = 0
    skipped: list[str] = field(default_factory=list)
    reasons: Counter[str] = field(default_factory=Counter)

    def line(self) -> str:
        return (
            f"ingest_rosters: {self.status} 組={self.pairs} 取得={self.fetched} "
            f"選手={self.players} 断面={self.seasons} "
            f"落とした選手={self.dropped} ポジション未登録={self.unregistered} "
            f"0件={self.empty} 失敗={self.failed}"
        )


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def start_year(label: str) -> int:
    """`seasons.label`（`2026-27`）の先頭4桁。

    **対応表を別に持たない**（詳細設計 1.2）。`select[year]` の値は開始年である。
    """
    head = label[:4]
    if not head.isdigit():
        raise RosterJobError("シーズンの label から開始年を読めない")
    return int(head)


def pairs_to_fetch(snapshot_dir: Path, season_id: str | None = None) -> list[Pair]:
    """取りに行く組を `club_seasons` と `seasons` から作る。

    **実在する組だけを返す。** `parse_roster` は0件を `ParseError` にするため、
    在籍していないクラブを渡すと「構造が変わった」と誤報する。
    """
    dataset = load_snapshot(snapshot_dir)
    labels = {
        str(r["id"]): str(r["label"])
        for r in dataset.table("seasons").to_dict("records")
    }
    out: list[Pair] = []
    for record in dataset.table("club_seasons").to_dict("records"):
        sid = str(record["season_id"])
        if season_id is not None and sid != season_id:
            continue
        label = labels.get(sid)
        if label is None:
            raise RosterJobError("club_seasons のシーズンが seasons に無い")
        out.append(Pair(season_id=sid, club_id=str(record["club_id"]), year=start_year(label)))
    out.sort(key=lambda p: (p.season_id, p.club_id))
    return out


def _payload(pair: Pair, entries: list[RosterEntry]) -> dict[str, object]:
    return rosters_payload(
        players=[{"id": e.player_id, "name": e.name} for e in entries],
        player_seasons=[
            {
                "playerId": e.player_id,
                "seasonId": pair.season_id,
                "clubId": pair.club_id,
                "number": e.number,
                "position": e.position,
            }
            for e in entries
        ],
    )


def run(
    *,
    client: Fetcher,
    api: Poster,
    snapshot_dir: Path = DEFAULT_SNAPSHOT,
    season_id: str | None = None,
    dry_run: bool = False,
    log: object = print,
) -> Result:
    """組ごとに取って、組ごとに1リクエスト送る。

    **組ごとに送る**理由は、失敗したクラブだけをやり直せることである（1シーズンを
    1リクエストに詰めても行数の上限には収まるが、1クラブの失敗で全体が落ちる）。
    """
    emit = log if callable(log) else print
    result = Result()
    pairs = pairs_to_fetch(snapshot_dir, season_id)
    result.pairs = len(pairs)
    if not pairs:
        raise RosterJobError("取りに行く組が1つもない")

    client.verify_policy(
        terms_reporter=report_terms_change(os.environ.get("SCRAPER_TERMS_SHA256", "")),
    )

    tracker = ParseFailureTracker()
    sent: list[dict[str, object]] = []
    try:
        for pair in pairs:
            try:
                roster = parse_roster(client.get(roster_url(pair.year, pair.club_id)))
            except ScrapingStopped:
                raise
            except (ParseError, ValidationError, ScraperError) as error:
                # **組を飛ばして続ける。** シーズンID・クラブID・理由を出す
                kind = type(error).__name__
                result.reasons[kind] += 1
                if isinstance(error, ParseError):
                    result.empty += 1
                else:
                    result.failed += 1
                result.skipped.append(f"{pair.season_id}/{pair.club_id}")
                emit(f"  skip {pair.season_id} club={pair.club_id} {kind}: {error}")
                tracker.failure()
                continue
            tracker.success()
            result.fetched += 1
            result.dropped += roster.dropped
            result.unregistered += roster.unregistered
            if roster.unregistered:
                # **未登録は落としていない。** 残したうえで件数を出す — 全員が
                # 未登録になったら（表記が変わった兆候）ここで分かる
                emit(
                    f"  note {pair.season_id} club={pair.club_id} "
                    f"ポジションが未登録の選手 {roster.unregistered}名を None で残した"
                )
            if roster.dropped:
                emit(
                    f"  note {pair.season_id} club={pair.club_id} "
                    f"ポジションを読めない選手 {roster.dropped}名を落とした"
                )
            body = _payload(pair, roster.entries)
            api.post("rosters", body)
            sent.append(body)
            result.players += len(roster.entries)
            result.seasons += len(roster.entries)
    except ScrapingStopped:
        # 429 / 503。**取得区間だけを中止する**（絶対ルール6）
        result.status = "PARTIAL"
        emit("ingest_rosters: 取得を中止した（相手側の指示）")
    except LoaderError as error:
        # **その場で止める。** 枠が尽きた状態では残りも全部失敗する（4.3）
        result.status = "PARTIAL"
        emit(f"ingest_rosters: D1 への書き込みに失敗した（{type(error).__name__}: {error}）")
    except ParseError as error:
        # 連続3件。遮断の疑いであり叩き続けない
        result.status = "PARTIAL"
        emit(f"ingest_rosters: 連続して失敗した（{type(error).__name__}: {error}）")

    if sent and not dry_run:
        # **スナップショットにも書く**（基本設計 2.2）。書かないと第1段から見えない
        _mirror(sent, snapshot_dir)
    if result.failed or result.empty:
        result.status = "PARTIAL"
    return result


def _mirror(bodies: list[dict[str, object]], snapshot_dir: Path) -> None:
    """送った本文そのものからスナップショットへ写す（詳細設計 4.2 のステップ2）。

    **別に組まない。** 片方だけ直したときに D1 とスナップショットが食い違う。
    """
    from batch.jobs.daily_ingest import apply_to_snapshot

    dataset = load_snapshot(snapshot_dir)
    apply_to_snapshot(dataset, snapshot_rows(*bodies))
    write_snapshot(dataset, snapshot_dir, tables=SNAPSHOT_TABLES)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="登録選手一覧を取り込む")
    parser.add_argument("--season", help="シーズンID（例 2026-27-PREMIER）。省略すると全季")
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--dry-run", action="store_true", help="D1 には書かない")
    args = parser.parse_args(argv)

    run_id = str(uuid.uuid4())
    started = _iso_now()
    api = InternalApi(
        os.environ.get("API_BASE_URL", ""),
        os.environ.get("INGEST_TOKEN", ""),
        dry_run=args.dry_run,
    )
    client = RateLimitedClient(
        user_agent=os.environ.get("SCRAPER_USER_AGENT", ""),
        state_path=Path(os.environ.get("SCRAPER_STATE_PATH", str(DEFAULT_STATE_PATH))),
        robots_sha256=os.environ.get("SCRAPER_ROBOTS_SHA256") or None,
        terms_sha256=os.environ.get("SCRAPER_TERMS_SHA256") or None,
    )
    try:
        result = run(client=client, api=api, snapshot_dir=args.snapshot,
                     season_id=args.season, dry_run=args.dry_run)
    except (RosterJobError, ScraperError, LoaderError) as error:
        print(f"ingest_rosters: 失敗（{type(error).__name__}: {error}）", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 — 公開ログの境界で本文を除去する
        print(f"ingest_rosters: 失敗（{type(error).__name__}）", file=sys.stderr)
        return 1

    print(result.line())
    if not args.dry_run:
        api.post("log", {
            "id": run_id, "job": "ingest_rosters", "startedAt": started,
            "finishedAt": _iso_now(), "status": result.status,
            "rowsAffected": result.players,
        })
    # **`PARTIAL` は exit 1。** exit 0 だと失敗通知に乗らない（絶対ルール6）
    return 1 if result.status == "PARTIAL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
