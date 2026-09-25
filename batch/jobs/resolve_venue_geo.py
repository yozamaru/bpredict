"""会場の座標を1回だけ解決して CSV に固定する（詳細設計 4.10）。

    python -m batch.jobs.resolve_venue_geo [--dry-run]   解決して CSV に書く
    python -m batch.jobs.resolve_venue_geo --load        CSV を D1 に送るだけ

**取り込みが全部終わってから流す。** 会場は取り込みとともに増えるため、途中で流すと
同じ会場を二度取りに行くことになる。

**埋められないときは埋めない**（住所がない / 候補が返らない / 都道府県が食い違う）。
飛ばした会場は件数と会場IDを出力に出す。座標が NULL でも先に進む — 使うのは
特徴量 #16（移動距離・検証区分）だけで、当該会場で欠損するだけである。
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from batch.geocode.gsi import Candidate, GeocodeError, candidates, sleep_between_requests
from batch.loader.api import InternalApi, LoaderError
from batch.loader.limits import chunks
from batch.parser.arena_parser import parse_arena_address, prefecture_of
from batch.parser.errors import DataUnavailable, ParseError, ValidationError
from batch.parser.terms import report_terms_change
from batch.scraper.arena import arena_detail_url
from batch.scraper.client import PolicyError, RateLimitedClient, ScraperError, ScrapingStopped

CSV_PATH = Path("db/seeds/master/venues_geo.csv")
DEFAULT_STATE_PATH = Path("batch/.scraper-state/state.json")
COLUMNS = ("venue_id", "name", "prefecture", "lat", "lng", "address", "source")

#: 住所 → 候補（関連度順）。差し替えられるようにして、テストで通信しない
type Geocoder = Callable[[str], list[Candidate]]


@dataclass
class Result:
    resolved: int = 0
    kept: int = 0
    #: (venue_id, 理由)。**件数だけでは調査ができない**（詳細設計 4.4 と同じ方針）
    skipped: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def read_csv(path: Path = CSV_PATH) -> dict[str, dict[str, str]]:
    """既存の CSV を会場IDで引ける形にする。**手で直した行を壊さない。**"""
    if not path.exists():
        return {}
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    known: dict[str, dict[str, str]] = {}
    for row in rows:
        venue_id = (row.get("venue_id") or "").strip()
        if not venue_id:
            raise ValidationError("venues_geo.csv に venue_id のない行がある")
        if venue_id in known:
            raise ValidationError(f"venues_geo.csv に venue_id の重複がある: {venue_id}")
        known[venue_id] = row
    return known


def write_csv(rows: dict[str, dict[str, str]], path: Path = CSV_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(COLUMNS))
        writer.writeheader()
        for venue_id in sorted(rows, key=lambda v: (len(v), v)):
            writer.writerow({key: rows[venue_id].get(key, "") for key in COLUMNS})


def pick(found: list[Candidate], prefecture: str | None) -> Candidate:
    """候補を選ぶ。**都道府県が食い違うものは採らない**（詳細設計 4.10）。

    候補は関連度順に並ぶが、1件目を無条件に採ると別の市区町村の座標が入りうる。
    """
    if not found:
        raise GeocodeError("住所検索が候補を返さなかった")
    if prefecture is None:
        raise GeocodeError("住所から都道府県が読めない")
    for candidate in found:
        if prefecture_of(candidate.title) == prefecture:
            return candidate
    raise GeocodeError(f"住所検索の候補の都道府県が住所と食い違う（{prefecture}）")


def resolve(
    *,
    client: RateLimitedClient,
    api: InternalApi,
    geocode: Geocoder = candidates,
    sleep: Callable[[], None] = sleep_between_requests,
    path: Path = CSV_PATH,
    dry_run: bool = False,
) -> Result:
    result = Result()

    client.verify_policy(
        terms_reporter=report_terms_change(os.environ.get("SCRAPER_TERMS_SHA256", "")),
    )
    known = read_csv(path)
    venues = api.get("venues", {"missingCoordinates": "1"})
    rows = venues["venues"] if isinstance(venues, dict) else []

    for row in rows:
        venue_id = str(row.get("id", ""))
        if not venue_id:
            raise ValidationError("会場の一覧に id のない行がある")
        if venue_id in known and known[venue_id].get("lat"):
            # 既に CSV にある会場は取りに行かない（1回だけ解決する）
            result.kept += 1
            continue
        try:
            found = parse_arena_address(client.get(arena_detail_url(venue_id)))
        except ScrapingStopped:
            result.notes.append("429/503 により取得区間を中止した")
            break
        except (DataUnavailable, ParseError, ValidationError, ScraperError) as error:
            result.skipped.append((venue_id, f"{type(error).__name__}: {error}"))
            continue
        try:
            best = pick(geocode(found.address), found.prefecture)
        except GeocodeError as error:
            result.skipped.append((venue_id, f"GeocodeError: {error}"))
            continue
        finally:
            sleep()
        known[venue_id] = {
            "venue_id": venue_id,
            "name": str(row.get("name", "")),
            "prefecture": found.prefecture or "",
            "lat": f"{best.lat:.6f}",
            "lng": f"{best.lng:.6f}",
            "address": found.address,
            "source": "国土地理院 住所検索（https://msearch.gsi.go.jp/）",
        }
        result.resolved += 1

    if not dry_run and result.resolved:
        write_csv(known, path)
    return result


def load(api: InternalApi, path: Path = CSV_PATH) -> int:
    """CSV を D1 に送る。`POST /internal/games` の `venues` 配列で受ける（詳細設計 3.4）。"""
    rows = read_csv(path)
    payload: list[dict[str, object]] = [
        {
            "id": venue_id,
            "name": row.get("name") or venue_id,
            "prefecture": row.get("prefecture") or None,
            "lat": float(row["lat"]) if row.get("lat") else None,
            "lng": float(row["lng"]) if row.get("lng") else None,
        }
        for venue_id, row in sorted(rows.items())
    ]
    sent = 0
    for part in chunks("venues", payload):
        api.post("games", {"venues": part})
        sent += len(part)
    return sent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="会場の座標を1回だけ解決する")
    parser.add_argument("--dry-run", action="store_true", help="解決するが CSV に書かない")
    parser.add_argument("--load", action="store_true", help="CSV を D1 に送るだけ（取得しない）")
    args = parser.parse_args(argv)

    try:
        api = InternalApi(
            os.environ.get("API_BASE_URL", ""), os.environ.get("INGEST_TOKEN", ""),
        )
        if args.load:
            print(f"resolve_venue_geo: D1 に送った会場={load(api)}")
            return 0
        client = RateLimitedClient(
            user_agent=os.environ.get("SCRAPER_USER_AGENT", ""),
            state_path=Path(os.environ.get("SCRAPER_STATE_PATH", str(DEFAULT_STATE_PATH))),
            robots_sha256=os.environ.get("SCRAPER_ROBOTS_SHA256") or None,
            terms_sha256=os.environ.get("SCRAPER_TERMS_SHA256") or None,
        )
        result = resolve(client=client, api=api, dry_run=args.dry_run)
    except PolicyError:
        print("resolve_venue_geo: 取得前確認に失敗した（robots / 利用規約）", file=sys.stderr)
        return 1
    except (LoaderError, ScraperError, ValidationError) as error:
        print(f"resolve_venue_geo: 失敗（{type(error).__name__}: {error}）", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 — 公開ログの境界で本文を除去する
        print(f"resolve_venue_geo: 失敗（{type(error).__name__}）", file=sys.stderr)
        return 1

    print(
        f"resolve_venue_geo: 解決={result.resolved} 既存={result.kept}"
        f" 未解決={len(result.skipped)}"
    )
    for note in result.notes:
        print(f"  - {note}")
    # **飛ばした会場は1行1件で出す。** 件数だけでは、どの会場がなぜ埋まらないかを
    # 調べるために実サイトへ取り直すことになる（詳細設計 4.4 と同じ方針）
    for venue_id, reason in result.skipped:
        print(f"  skip {venue_id} {reason}")
    return 1 if result.notes else 0


if __name__ == "__main__":
    raise SystemExit(main())
