"""登録選手一覧の取り込み（詳細設計 4.13）。合成データだけで検証する。"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path

import pandas as pd
import pytest

from batch.features.dataset import export_sqlite, load_snapshot, write_snapshot
from batch.jobs import ingest_rosters
from batch.loader.api import LoaderError
from batch.loader.payload import PayloadError, rosters_payload, snapshot_rows
from batch.scraper.client import ScrapingStopped
from batch.tests.fixtures.roster import card, page


class FakeClient:
    """`verify_policy` と `get` だけを持つ。取得の作法は client 側で検証済み。"""

    def __init__(self, bodies: dict[str, str] | None = None,
                 raises: dict[str, Exception] | None = None) -> None:
        self.bodies = bodies or {}
        self.raises = raises or {}
        self.urls: list[str] = []
        self.verified = 0

    def verify_policy(self, terms_reporter: object = None) -> None:
        self.verified += 1

    def get(self, url: str) -> str:
        self.urls.append(url)
        for needle, error in self.raises.items():
            if needle in url:
                raise error
        for needle, body in self.bodies.items():
            if needle in url:
                return body
        return page()


class FakeApi:
    def __init__(self, fail_on: int | None = None) -> None:
        self.sent: list[tuple[str, dict[str, object]]] = []
        self.fail_on = fail_on

    def post(self, path: str, payload: Mapping[str, object]) -> object:
        if self.fail_on is not None and len(self.sent) == self.fail_on:
            raise LoaderError("書き込み枠が尽きた")
        self.sent.append((path, dict(payload)))
        return {}


def build_snapshot(db: sqlite3.Connection, tmp_path: Path,
                   pairs: list[tuple[str, str]],
                   labels: dict[str, str] | None = None) -> Path:
    """シードから作ったスナップショットの `seasons` / `club_seasons` を差し替える。

    **列の揃った全テーブルが要る** — `load_snapshot` は `games.finished_at` などを
    読み、`write_snapshot` は MANIFEST をディスクの現物から作り直す。空の
    DataFrame を並べるだけでは成立しない。
    """
    labels = labels or {sid: sid[:7] for sid, _ in pairs}
    dataset = export_sqlite(db)
    dataset.tables["seasons"] = pd.DataFrame([
        {"id": sid, "label": label, "league": "PREMIER",
         "start_date": f"{label[:4]}-09-01", "end_date": f"{int(label[:4]) + 1}-06-30"}
        for sid, label in sorted(labels.items())
    ])
    dataset.tables["club_seasons"] = pd.DataFrame([
        {"club_id": club, "season_id": sid, "name": f"架空{club}",
         "short_name": club, "league": "PREMIER", "primary_venue_id": None,
         "color_primary": None, "color_secondary": None}
        for sid, club in pairs
    ])
    # 取り込み前の状態にする（空だが列は揃っている）
    for name in ("players", "player_seasons"):
        dataset.tables[name] = dataset.table(name).iloc[0:0]
    write_snapshot(dataset, tmp_path)
    return tmp_path


# --- 取りに行く組の決め方 ---


def test_the_start_year_comes_from_the_season_label() -> None:
    """**対応表を別に持たない**（詳細設計 1.2）。"""
    assert ingest_rosters.start_year("2026-27") == 2026
    assert ingest_rosters.start_year("2016-17") == 2016


def test_a_label_without_a_year_is_an_error() -> None:
    with pytest.raises(ingest_rosters.RosterJobError):
        ingest_rosters.start_year("premier")


def test_pairs_come_from_club_seasons(seeded_db: sqlite3.Connection, tmp_path: Path) -> None:
    """**既に持っている情報を使う。** `select[club]` を読むための11回を節約する。"""
    build_snapshot(seeded_db, tmp_path, [("2026-27-PREMIER", "704"), ("2026-27-PREMIER", "703")])
    pairs = ingest_rosters.pairs_to_fetch(tmp_path)
    assert [(p.season_id, p.club_id, p.year) for p in pairs] == [
        ("2026-27-PREMIER", "703", 2026),
        ("2026-27-PREMIER", "704", 2026),
    ]


def test_a_season_filter_narrows_the_pairs(seeded_db: sqlite3.Connection, tmp_path: Path) -> None:
    build_snapshot(seeded_db, tmp_path, [("2026-27-PREMIER", "704"), ("2016-17-B1", "703")],
                   labels={"2026-27-PREMIER": "2026-27", "2016-17-B1": "2016-17"})
    pairs = ingest_rosters.pairs_to_fetch(tmp_path, "2016-17-B1")
    assert [(p.season_id, p.club_id, p.year) for p in pairs] == [("2016-17-B1", "703", 2016)]


def test_a_season_missing_from_seasons_is_an_error(seeded_db: sqlite3.Connection, tmp_path: Path) -> None:
    """**黙って飛ばさない。** `club_seasons` にあるのに `seasons` に無いのは欠陥である。"""
    build_snapshot(seeded_db, tmp_path, [("2026-27-PREMIER", "704")],
                   labels={"2026-27-PREMIER": "2026-27"})
    ds = load_snapshot(tmp_path)
    ds.tables["seasons"] = pd.DataFrame(columns=["id", "label", "league", "start_date", "end_date"])
    write_snapshot(ds, tmp_path, tables=("seasons",))
    with pytest.raises(ingest_rosters.RosterJobError):
        ingest_rosters.pairs_to_fetch(tmp_path)


# --- 送る本文 ---


def test_the_body_carries_only_the_four_fields(seeded_db: sqlite3.Connection, tmp_path: Path) -> None:
    """**出せないものはキーを送らない**（要件 5.3）。"""
    build_snapshot(seeded_db, tmp_path, [("2026-27-PREMIER", "704")])
    client, api = FakeClient(), FakeApi()
    ingest_rosters.run(client=client, api=api, snapshot_dir=tmp_path, log=lambda _m: None)
    _, body = api.sent[0]
    assert sorted(body) == ["playerSeasons", "players"]
    assert sorted(body["players"][0]) == ["id", "name"]        # type: ignore[index]
    assert sorted(body["playerSeasons"][0]) == [              # type: ignore[index]
        "clubId", "number", "playerId", "position", "seasonId",
    ]


def test_an_empty_body_is_rejected() -> None:
    with pytest.raises(PayloadError):
        rosters_payload(players=[], player_seasons=[])


def test_one_request_per_pair(seeded_db: sqlite3.Connection, tmp_path: Path) -> None:
    """**組ごとに送る。** 失敗したクラブだけをやり直せる（詳細設計 4.13）。"""
    build_snapshot(seeded_db, tmp_path, [("2026-27-PREMIER", "704"), ("2026-27-PREMIER", "703")])
    client, api = FakeClient(), FakeApi()
    result = ingest_rosters.run(client=client, api=api, snapshot_dir=tmp_path,
                                log=lambda _m: None)
    assert len(api.sent) == 2
    assert [p for p, _ in api.sent] == ["rosters", "rosters"]
    assert (result.pairs, result.fetched, result.status) == (2, 2, "SUCCESS")


def test_the_policy_gate_runs_before_any_page(seeded_db: sqlite3.Connection, tmp_path: Path) -> None:
    """robots と利用規約の照合を通さずに試合データを取らない（絶対ルール6）。"""
    build_snapshot(seeded_db, tmp_path, [("2026-27-PREMIER", "704")])
    client, api = FakeClient(), FakeApi()
    ingest_rosters.run(client=client, api=api, snapshot_dir=tmp_path, log=lambda _m: None)
    assert client.verified == 1


# --- 失敗したときの挙動（詳細設計 4.13） ---


def test_a_pair_that_cannot_be_parsed_is_skipped_not_fatal(seeded_db: sqlite3.Connection, tmp_path: Path) -> None:
    """**組を飛ばして続ける。** 1クラブで全体を落とさない。"""
    build_snapshot(seeded_db, tmp_path, [("2026-27-PREMIER", "704"), ("2026-27-PREMIER", "703")])
    client = FakeClient(bodies={"club=703": page([])})   # 0件 → ParseError
    api = FakeApi()
    lines: list[str] = []
    result = ingest_rosters.run(client=client, api=api, snapshot_dir=tmp_path,
                                log=lines.append)
    assert result.fetched == 1
    assert result.empty == 1
    assert result.status == "PARTIAL"
    assert result.skipped == ["2026-27-PREMIER/703"]
    # **シーズンID・クラブID・理由を出す**（件数だけでは調査できない）
    assert any("club=703" in line and "ParseError" in line for line in lines)


def test_dropped_players_are_reported_not_hidden(seeded_db: sqlite3.Connection, tmp_path: Path) -> None:
    """**黙って減らさない**（詳細設計 4.13）。"""
    build_snapshot(seeded_db, tmp_path, [("2026-27-PREMIER", "704")])
    body = page([card("9001", "架空選手一", "SG #8"), card("9002", "架空選手二", "G #9")])
    client, api = FakeClient(bodies={"club=704": body}), FakeApi()
    lines: list[str] = []
    result = ingest_rosters.run(client=client, api=api, snapshot_dir=tmp_path,
                                log=lines.append)
    assert (result.players, result.dropped) == (1, 1)
    assert any("落とした" in line for line in lines)


def test_a_remote_stop_ends_the_fetch_but_keeps_what_was_sent(seeded_db: sqlite3.Connection, tmp_path: Path) -> None:
    """429 / 503 は**取得区間だけを中止する**（絶対ルール6）。"""
    build_snapshot(seeded_db, tmp_path, [("2026-27-PREMIER", "703"), ("2026-27-PREMIER", "704")])
    client = FakeClient(raises={"club=704": ScrapingStopped("stopped")})
    api = FakeApi()
    result = ingest_rosters.run(client=client, api=api, snapshot_dir=tmp_path,
                                log=lambda _m: None)
    assert result.status == "PARTIAL"
    assert result.fetched == 1          # 703 は送れている
    assert len(api.sent) == 1


def test_a_write_failure_stops_on_the_spot(seeded_db: sqlite3.Connection, tmp_path: Path) -> None:
    """**枠が尽きた状態では残りも全部失敗する**（詳細設計 4.3）。"""
    build_snapshot(seeded_db, tmp_path, [
        ("2026-27-PREMIER", "701"), ("2026-27-PREMIER", "703"), ("2026-27-PREMIER", "704"),
    ])
    client, api = FakeClient(), FakeApi(fail_on=1)
    result = ingest_rosters.run(client=client, api=api, snapshot_dir=tmp_path,
                                log=lambda _m: None)
    assert result.status == "PARTIAL"
    # 1件送ってから落ちる。3組すべてを叩き続けない
    assert len(api.sent) == 1
    assert len(client.urls) == 2


def test_no_pair_at_all_is_an_error(seeded_db: sqlite3.Connection, tmp_path: Path) -> None:
    build_snapshot(seeded_db, tmp_path, [("2026-27-PREMIER", "704")])
    with pytest.raises(ingest_rosters.RosterJobError):
        ingest_rosters.run(client=FakeClient(), api=FakeApi(), snapshot_dir=tmp_path,
                           season_id="2099-00-PREMIER", log=lambda _m: None)


# --- スナップショットにも書く（基本設計 2.2） ---


def test_the_snapshot_receives_the_same_rows(seeded_db: sqlite3.Connection, tmp_path: Path) -> None:
    """**書かないと第1段から見えない。** 入力はスナップショットだけである。"""
    build_snapshot(seeded_db, tmp_path, [("2026-27-PREMIER", "704")])
    ingest_rosters.run(client=FakeClient(), api=FakeApi(), snapshot_dir=tmp_path,
                       log=lambda _m: None)
    ds = load_snapshot(tmp_path)
    players = ds.table("players")
    seasons = ds.table("player_seasons")
    assert len(players) == 4
    assert len(seasons) == 4
    assert sorted(seasons["position"]) == ["C", "PG", "SG", "SG"]
    assert set(seasons["season_id"]) == {"2026-27-PREMIER"}
    assert set(seasons["club_id"]) == {"704"}


def test_dry_run_leaves_the_snapshot_alone(seeded_db: sqlite3.Connection, tmp_path: Path) -> None:
    build_snapshot(seeded_db, tmp_path, [("2026-27-PREMIER", "704")])
    ingest_rosters.run(client=FakeClient(), api=FakeApi(), snapshot_dir=tmp_path,
                       dry_run=True, log=lambda _m: None)
    assert len(load_snapshot(tmp_path).table("players")) == 0


def test_player_seasons_has_a_snapshot_destination() -> None:
    """**写し先が未定の配列は `PayloadError` で止まる**（詳細設計 4.2 のステップ2）。

    `playerSeasons` を写し先に登録し忘れると、D1 にだけ入る（座標140件と同じ形）。
    """
    rows = snapshot_rows({
        "players": [{"id": "p-1", "name": "x"}],
        "playerSeasons": [{"playerId": "p-1", "seasonId": "s", "clubId": "c"}],
    })
    assert sorted(rows) == ["player_seasons", "players"]
    assert rows["player_seasons"][0] == {
        "player_id": "p-1", "season_id": "s", "club_id": "c",
    }
