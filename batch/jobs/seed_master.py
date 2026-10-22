"""マスタ投入（工程2）。

投入するのは**事前に列挙できるもの**だけである。

- `seasons`          … 11行。`db/seeds/master/seasons.csv`
- `clubs`            … 30行。`db/seeds/master/clubs.csv`
- `club_source_ids`  … 30行。`db/seeds/master/club_source_ids.csv`

`club_seasons` と会場マスタは含めない。どちらも集合が事前に列挙できず、
backfill が試合データから作る（`docs/design-detail.md` 1.2 / 4.4）。

書き込みは Workers の `POST /internal/masters` 経由に一本化する。D1 REST API を
直接叩かない（CLAUDE.md 絶対ルール3）。
"""
from __future__ import annotations

import csv
import dataclasses
import json
import os
import pathlib
import re
import urllib.error
import urllib.request

from batch.loader.api import INTERNAL_USER_AGENT

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SEED_DIR = REPO_ROOT / "db" / "seeds" / "master"

LEAGUES = ("B1", "B2", "B3", "PREMIER", "ONE", "NEXT")
SLUG_RE = re.compile(r"^[a-z0-9-]{1,40}$")          # 詳細設計 3.2
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class SeedError(Exception):
    """シードデータが不正。**握りつぶさずに落とす。**

    マスタは手で用意したデータであり、壊れていれば直すべきものである。
    スキップすると FK 違反が後段で出て、原因が分かりにくくなる。
    """


@dataclasses.dataclass(frozen=True)
class Season:
    """`seasons` の1行（詳細設計 1.1）。

    **`id` にリーグを含める**（`2026-27-PREMIER`）。`2026-27` 単独では同一シーズンの
    PREMIER と ONE を同時に持てず、B.ONE 拡張時に主キー変更＝全ファクトテーブルの
    FK 移行になる。

    **開始日・終了日は CSV に固定した値である**（当季の9月1日〜翌年6月30日）。
    日程一覧から導出する方式は CS を取りこぼし、**実在する試合日が404になった**。
    """

    id: str
    label: str
    league: str
    start_date: str
    end_date: str


@dataclasses.dataclass(frozen=True)
class Club:
    """`clubs` の1行。**恒久エンティティであり、改称でも変わらない。**

    `slug` は運営者が決めた値で、**改称しても変えない**（URL の安定を優先する。
    詳細設計 1.1）。`name` は現在の表示名で、各季の表示名は `club_seasons.name` が
    持つ — したがって `703` の slug と現在の表示名が一致しない状態が生じうる。
    """

    id: str
    slug: str
    name: str


@dataclasses.dataclass(frozen=True)
class ClubSourceId:
    """公式サイトのチームID → `club_id` の対応（詳細設計 1.2）。

    **公式の `TeamID` はシーズン・改称・リーグ再編をまたいで不変である**
    （Phase 0 で確認）ため本ソースでは1:1になるが、**ID 体系が変わったときの
    継ぎ目として維持する。**

    `valid_from` / `valid_to` で期間を持つのは、将来 ID が振り直されたときに
    期間で解決できるようにするためである。
    """

    source_id: str
    club_id: str
    valid_from: str
    valid_to: str
    note: str | None


def _rows(path: pathlib.Path) -> list[dict[str, str]]:
    if not path.exists():
        raise SeedError(f"シードファイルがない: {path.name}")
    with path.open(encoding="utf-8", newline="") as f:
        rows = [{k: (v or "").strip() for k, v in r.items()} for r in csv.DictReader(f)]
    if not rows:
        raise SeedError(f"シードファイルが空: {path.name}")
    return rows


def _date(value: str, where: str) -> str:
    if not DATE_RE.match(value):
        raise SeedError(f"{where}: 日付が YYYY-MM-DD でない: {value!r}")
    return value


def load_seasons(seed_dir: pathlib.Path = SEED_DIR) -> list[Season]:
    """`seasons.csv` を読む。**DDL の CHECK と同じことを投入前に確かめる。**

    `league` が CHECK 制約の集合外、`start_date > end_date`、`id` の重複はいずれも
    `SeedError` で落とす — **D1 に送ってから落ちるより手元で落ちる方がよい**
    （送れば書き込み枠を使い、枠は 00:00 UTC にしか戻らない）。
    """
    out = []
    for r in _rows(seed_dir / "seasons.csv"):
        if r["league"] not in LEAGUES:
            raise SeedError(f"seasons: league が CHECK 制約外: {r['league']!r}")
        start = _date(r["start_date"], f"seasons {r['id']}")
        end = _date(r["end_date"], f"seasons {r['id']}")
        if start > end:
            raise SeedError(f"seasons {r['id']}: start_date > end_date")
        out.append(Season(r["id"], r["label"], r["league"], start, end))
    ids = [s.id for s in out]
    if len(set(ids)) != len(ids):
        raise SeedError("seasons: id が重複している")
    return out


def load_clubs(seed_dir: pathlib.Path = SEED_DIR) -> list[Club]:
    """`clubs.csv` を読む。**`slug` の形式と重複をここで確かめる**（詳細設計 1.1）。"""
    out = []
    for r in _rows(seed_dir / "clubs.csv"):
        if not SLUG_RE.match(r["slug"]):
            raise SeedError(f"clubs {r['id']}: slug が ^[a-z0-9-]{{1,40}}$ に合わない: {r['slug']!r}")
        if not r["name"]:
            raise SeedError(f"clubs {r['id']}: name が空")
        out.append(Club(r["id"], r["slug"], r["name"]))
    for key, label in ((lambda c: c.id, "id"), (lambda c: c.slug, "slug")):
        vals = [key(c) for c in out]
        if len(set(vals)) != len(vals):
            raise SeedError(f"clubs: {label} が重複している")
    return out


def load_club_source_ids(seed_dir: pathlib.Path = SEED_DIR) -> list[ClubSourceId]:
    """`club_source_ids.csv` を読む。**`source_id` の重複を落とす。**

    重複を通すと同じ公式IDが2つのクラブに解決しうる状態になり、取り込みが
    どちらを選ぶかで `team_ratings` が分断される。
    """
    out = []
    for r in _rows(seed_dir / "club_source_ids.csv"):
        out.append(ClubSourceId(
            r["source_id"], r["club_id"],
            _date(r["valid_from"], f"club_source_ids {r['source_id']}"),
            _date(r["valid_to"], f"club_source_ids {r['source_id']}"),
            r.get("note") or None,
        ))
    sids = [c.source_id for c in out]
    if len(set(sids)) != len(sids):
        raise SeedError("club_source_ids: source_id が重複している")
    return out


def load_all(seed_dir: pathlib.Path = SEED_DIR) -> tuple[list[Season], list[Club], list[ClubSourceId]]:
    """3つの CSV をまとめて読む。**どれか1つでも壊れていれば何も送らない。**"""
    seasons, clubs, source_ids = (
        load_seasons(seed_dir), load_clubs(seed_dir), load_club_source_ids(seed_dir))
    known = {c.id for c in clubs}
    missing = sorted({s.club_id for s in source_ids} - known, key=str)
    if missing:
        raise SeedError(f"club_source_ids が clubs にない club_id を指している: {missing}")
    unmapped = sorted(known - {s.club_id for s in source_ids}, key=str)
    if unmapped:
        raise SeedError(f"clubs に対応する club_source_ids がない: {unmapped}")
    return seasons, clubs, source_ids


def build_payload(
    seasons: list[Season],
    clubs: list[Club],
    source_ids: list[ClubSourceId],
) -> dict[str, list[dict[str, object]]]:
    """`POST /internal/masters` のボディ。名前付き配列で送る（詳細設計 3.4）。"""
    return {
        "seasons": [
            {"id": s.id, "label": s.label, "league": s.league,
             "startDate": s.start_date, "endDate": s.end_date} for s in seasons],
        "clubs": [{"id": c.id, "slug": c.slug, "name": c.name} for c in clubs],
        "clubSourceIds": [
            {"sourceId": s.source_id, "clubId": s.club_id,
             "validFrom": s.valid_from, "validTo": s.valid_to, "note": s.note}
            for s in source_ids],
    }


def post(payload: dict, *, base_url: str, token: str, dry_run: bool = False) -> None:
    """`POST /internal/masters` へ送る。**D1 REST API を直接叩かない**（絶対ルール3）。

    **User-Agent を明示する。** urllib の既定（`Python-urllib/3.12`）は Cloudflare に
    403 で弾かれる。403 は Worker のコードが返さない値であるため、アプリのログにも
    `ingestion_logs` にも原因が残らず、**「トークンが違う」と誤診しやすい**
    （詳細設計 1.20）。

    `dry_run` のときは件数だけを出して送らない。
    """
    counts = {k: len(v) for k, v in payload.items()}
    if dry_run:
        print(f"[dry-run] POST /internal/masters {counts}")
        return
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/v1/internal/masters",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            # urllib の既定 UA は Cloudflare に 403 で弾かれる（batch/loader/api.py）
            "User-Agent": INTERNAL_USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
    except urllib.error.HTTPError as e:
        # 例外オブジェクトをそのまま出さない（URLにIDが含まれる。絶対ルール4）
        raise SeedError(f"POST /internal/masters が {e.code} を返した") from None
    except urllib.error.URLError:
        raise SeedError("POST /internal/masters に到達できなかった") from None
    print(f"POST /internal/masters {counts}")


def main(argv: list[str] | None = None) -> int:
    """マスタを投入する。**冪等である**（`ON CONFLICT DO UPDATE`）。

    71行すべてが1リクエスト（5文）に収まる（詳細設計 3.4）。運用手順では
    backfill の前に毎回流すため（`club_source_ids` が無いとクラブを解決できない）、
    何度流しても同じ結果になることが要る。
    """
    import argparse

    ap = argparse.ArgumentParser(description="マスタを投入する（冪等）")
    ap.add_argument("--dry-run", action="store_true", help="検証と件数表示のみ。送信しない")
    ap.add_argument("--seed-dir", type=pathlib.Path, default=SEED_DIR)
    args = ap.parse_args(argv)

    seasons, clubs, source_ids = load_all(args.seed_dir)
    payload = build_payload(seasons, clubs, source_ids)
    post(payload,
         base_url=os.environ.get("API_BASE_URL", ""),
         token=os.environ.get("INGEST_TOKEN", ""),
         dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
