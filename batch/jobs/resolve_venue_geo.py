"""会場の座標を1回だけ解決して CSV に固定する（詳細設計 4.10）。

    python -m batch.jobs.resolve_venue_geo [--dry-run]   解決して CSV に書く
    python -m batch.jobs.resolve_venue_geo --sync-snapshot    CSV をスナップショットに反映する
    python -m batch.jobs.resolve_venue_geo --load        反映して D1 にも送る

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

from batch.features.dataset import Dataset, load_snapshot, write_snapshot
from batch.geocode.gsi import Candidate, GeocodeError, candidates, sleep_between_requests
from batch.loader.api import InternalApi, LoaderError
from batch.loader.limits import chunks
from batch.parser.arena_parser import (
    municipality_of,
    parse_arena_address,
    prefecture_of,
)
from batch.parser.errors import DataUnavailable, ParseError, ValidationError
from batch.parser.terms import report_terms_change
from batch.scraper.arena import arena_detail_url
from batch.scraper.client import PolicyError, RateLimitedClient, ScraperError, ScrapingStopped

CSV_PATH = Path("db/seeds/master/venues_geo.csv")
DEFAULT_SNAPSHOT = Path("batch/snapshot")
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


def pick(found: list[Candidate], address: str) -> tuple[Candidate, str]:
    """候補を選び、都道府県を決める（詳細設計 4.10）。

    候補は関連度順に並ぶが、**1件目を無条件に採ると別の市区町村の座標が入りうる**。
    照合の手がかりは住所の書き方で変わる。

    - 住所に都道府県がある → それと一致する候補を採る
    - 住所に都道府県がない（公式サイトに実在する） → **候補の `title` から読む。**
      ただし**住所の先頭の市区町村が候補に含まれること**を確認する

    **都道府県を市区町村名から推測しない。** 同名の市区町村が複数の県にあると誤る。
    出典（国土地理院の候補）に書いてある値を読むのであって、こちらで決めない。
    """
    if not found:
        raise GeocodeError("住所検索が候補を返さなかった")

    prefecture = prefecture_of(address)
    if prefecture is not None:
        for candidate in found:
            if prefecture_of(candidate.title) == prefecture:
                return candidate, prefecture
        raise GeocodeError(f"住所検索の候補の都道府県が住所と食い違う（{prefecture}）")

    municipality = municipality_of(address)
    if municipality is None:
        raise GeocodeError("住所から市区町村が読めない")

    # **飛ばす理由を混ぜない**（詳細設計 4.4 と同じ方針）。市区町村が候補に現れない
    # のと、現れたのに候補から都道府県が読めないのは、調べる先が違う。
    matched_without_prefecture = False
    for candidate in found:
        if municipality not in candidate.title:
            continue
        resolved = prefecture_of(candidate.title)
        if resolved is not None:
            return candidate, resolved
        matched_without_prefecture = True
    if matched_without_prefecture:
        raise GeocodeError(f"候補から都道府県が読めない（{municipality}）")
    raise GeocodeError(f"住所検索の候補に市区町村が現れない（{municipality}）")


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
            best, prefecture = pick(geocode(found.address), found.address)
        except GeocodeError as error:
            result.skipped.append((venue_id, f"GeocodeError: {error}"))
            continue
        finally:
            sleep()
        known[venue_id] = {
            "venue_id": venue_id,
            "name": str(row.get("name", "")),
            # **候補から読んだ値を入れる。** 住所に都道府県がない会場では
            # `found.prefecture` は None だが、候補の `title` には入っている（4.10）
            "prefecture": prefecture,
            "lat": f"{best.lat:.6f}",
            "lng": f"{best.lng:.6f}",
            "address": found.address,
            "source": "国土地理院 住所検索（https://msearch.gsi.go.jp/）",
        }
        result.resolved += 1

    if not dry_run and result.resolved:
        write_csv(known, path)
    return result


def sync_snapshot(
    path: Path = CSV_PATH, snapshot_dir: Path = DEFAULT_SNAPSHOT,
) -> int:
    """CSV の座標をスナップショットの `venues` に反映する（詳細設計 4.10 の段5）。

    **D1 を要しない。** 出典はコミット済みの CSV で、適用は決定論的である。

    **この段を飛ばすと座標は特徴量から見えない。** スナップショットが特徴量・学習・
    推論の唯一の入力であり（絶対ルール3）、D1 は公開APIの表示用の複製である。
    2026-10-03 までこの段がなく、D1 に 140件あるのにスナップショットは 0件だった。

    **`name` は上書きしない。** 初出の名称で固定する（詳細設計 1.2）。CSV の `name` は
    解決したときの表示名であって、`venues.name` の出典ではない。
    """
    rows = read_csv(path)
    dataset = load_snapshot(snapshot_dir)
    venues = dataset.table("venues").copy()
    for column in ("prefecture", "lat", "lng"):
        if column not in venues.columns:
            raise ValidationError(f"スナップショットの venues に {column} がない")

    # **座標が揃っていない行は使わない。** 都道府県だけ入れて座標を空にすると、
    # #16 が「座標がある会場」と誤って数える余地が生まれる
    resolved = {
        venue_id: _geo_of(row)
        for venue_id, row in rows.items()
        if row.get("lat") and row.get("lng")
    }
    ids = list(venues["id"].astype(str))
    # **CSV に行がない会場は触らない。** 既にある値を None で上書きしない
    venues["prefecture"] = [
        resolved[i].prefecture if i in resolved else old
        for i, old in zip(ids, venues["prefecture"], strict=True)
    ]
    venues["lat"] = [
        resolved[i].lat if i in resolved else old
        for i, old in zip(ids, venues["lat"], strict=True)
    ]
    venues["lng"] = [
        resolved[i].lng if i in resolved else old
        for i, old in zip(ids, venues["lng"], strict=True)
    ]
    updated = sum(1 for i in ids if i in resolved)
    write_snapshot(
        Dataset(tables={**dataset.tables, "venues": venues}),
        snapshot_dir,
        tables=["venues"],
    )
    return updated


@dataclass(frozen=True)
class Geo:
    """CSV の1行から取り出した座標と都道府県。**空文字と欠損を混ぜない。**"""

    prefecture: str | None
    lat: float | None
    lng: float | None


def _geo_of(row: dict[str, str]) -> Geo:
    return Geo(
        prefecture=row.get("prefecture") or None,
        lat=float(row["lat"]) if row.get("lat") else None,
        lng=float(row["lng"]) if row.get("lng") else None,
    )


def load(api: InternalApi, path: Path = CSV_PATH) -> int:
    """CSV を D1 に送る。`POST /internal/games` の `venues` 配列で受ける（詳細設計 3.4）。"""
    rows = read_csv(path)
    payload: list[dict[str, object]] = [
        {
            "id": venue_id,
            "name": row.get("name") or venue_id,
            "prefecture": geo.prefecture,
            "lat": geo.lat,
            "lng": geo.lng,
        }
        for venue_id, row in sorted(rows.items())
        if (geo := _geo_of(row)) is not None
    ]
    sent = 0
    for part in chunks("venues", payload):
        api.post("games", {"venues": part})
        sent += len(part)
    return sent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="会場の座標を1回だけ解決する")
    parser.add_argument("--dry-run", action="store_true", help="解決するが CSV に書かない")
    parser.add_argument("--load", action="store_true", help="反映して D1 にも送る（取得しない）")
    parser.add_argument(
        "--sync-snapshot", action="store_true",
        help="CSV をスナップショットに反映するだけ（D1 に送らない。取得しない）",
    )
    args = parser.parse_args(argv)

    try:
        # **スナップショットへの反映は D1 を要しない。** 内部APIは生成時に接続先を
        # 検証するため、`--sync-snapshot` では作らない（`derive_primary_venues` と同じ）
        if args.sync_snapshot:
            print(f"resolve_venue_geo: スナップショットに反映した会場={sync_snapshot()}")
            return 0
        api = InternalApi(
            os.environ.get("API_BASE_URL", ""), os.environ.get("INGEST_TOKEN", ""),
        )
        if args.load:
            # **スナップショットを先に書く**（基本設計 2.2 の順序）
            reflected = sync_snapshot()
            print(
                f"resolve_venue_geo: スナップショット={reflected}"
                f" D1 に送った会場={load(api)}"
            )
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
