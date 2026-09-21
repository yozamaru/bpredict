"""工程2: マスタ投入の検証。

`db/seeds/master/*.csv` が制約を満たし、実際のスキーマに入り、
旧B1と新リーグのクラブが `club_source_ids` で紐付くことを確かめる。

D1 への実投入は工程4（`POST /internal/masters`）の後に行うため、ここでは
in-memory SQLite に対して検証する（`docs/design-detail.md` 9章 工程2）。
"""
from __future__ import annotations

import csv
import itertools
import pathlib
import re
import sqlite3
from collections.abc import Iterator

import pytest

from batch.jobs.seed_master import (
    SeedError,
    build_payload,
    load_all,
    load_club_source_ids,
    load_clubs,
    load_seasons,
)
from batch.tests.conftest import apply_migrations

EXPECTED_SEASONS = 11          # 2016-17 〜 2026-27
EXPECTED_CLUBS = 30           # トップリーグに現れた TeamID（verification/RESULTS.md）
GONE_FROM_PREMIER = {"695", "717", "745", "753"}


# --- シードデータそのもの -------------------------------------------------------


def test_seed_files_load_and_cross_reference():
    """3ファイルが読めて、相互参照が閉じていること。"""
    seasons, clubs, source_ids = load_all()
    assert len(seasons) == EXPECTED_SEASONS
    assert len(clubs) == EXPECTED_CLUBS
    assert len(source_ids) == EXPECTED_CLUBS


def test_season_ids_carry_league():
    """`seasons.id` にリーグが入っていること（詳細設計 1.1）。

    '2026-27' 単独だと同一シーズンの PREMIER と ONE を同時に持てない。
    """
    for s in load_seasons():
        assert s.id.endswith(("-B1", "-PREMIER")), s.id
        assert s.id.startswith(s.label), (s.id, s.label)


def test_season_ranges_are_ordered_and_disjoint():
    """範囲が前後せず、シーズン同士が重ならないこと。"""
    seasons = sorted(load_seasons(), key=lambda s: s.start_date)
    for s in seasons:
        assert s.start_date < s.end_date, s.id
    for a, b in itertools.pairwise(seasons):
        assert a.end_date < b.start_date, f"{a.id} と {b.id} の範囲が重なっている"


def test_season_range_contains_known_game_dates():
    """実在する試合日が範囲に入っていること。

    範囲が狭いと、その日付の URL が 404 になる（詳細設計 1.1 / 3.5）。
    """
    known = {"2016-17-B1": "2016-09-22"}     # B.LEAGUE 開幕日
    by_id = {s.id: s for s in load_seasons()}
    for season_id, date in known.items():
        s = by_id[season_id]
        assert s.start_date <= date <= s.end_date, (season_id, date)


def test_club_slug_format_and_uniqueness():
    """slug が形式を満たし重複しないこと（詳細設計 3.2 / 1.1）。"""
    clubs = load_clubs()
    slugs = [c.slug for c in clubs]
    assert len(set(slugs)) == len(slugs)
    for c in clubs:
        assert re.fullmatch(r"[a-z0-9-]{1,40}", c.slug), (c.id, c.slug)
        assert not c.slug.startswith("-") and not c.slug.endswith("-"), c.slug
        assert "--" not in c.slug, c.slug


def test_club_source_ids_are_one_to_one():
    """公式IDとクラブが1:1であること。

    `TeamID` は改称・リーグ再編をまたいで不変（Phase 0 で確認）なので
    このソースでは1:1になる。表は ID体系が変わったときの継ぎ目として持つ。
    """
    source_ids = load_club_source_ids()
    assert len({s.source_id for s in source_ids}) == len(source_ids)
    assert len({s.club_id for s in source_ids}) == len(source_ids)


def test_clubs_include_those_gone_from_premier():
    """2026-27 の B.PREMIER にいない4クラブも `clubs` にあること。

    旧B1の試合データに現れるため、なければ取り込みが FK 違反で落ちる。
    """
    ids = {c.id for c in load_clubs()}
    assert ids >= GONE_FROM_PREMIER


# --- 実際のスキーマに入るか -----------------------------------------------------


@pytest.fixture
def seeded() -> Iterator[sqlite3.Connection]:
    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA foreign_keys = ON")
    apply_migrations(con)
    seasons, clubs, source_ids = load_all()
    con.executemany(
        "INSERT INTO seasons (id, label, league, start_date, end_date) VALUES (?,?,?,?,?)",
        [(s.id, s.label, s.league, s.start_date, s.end_date) for s in seasons])
    con.executemany("INSERT INTO clubs (id, slug, name) VALUES (?,?,?)",
                    [(c.id, c.slug, c.name) for c in clubs])
    con.executemany(
        "INSERT INTO club_source_ids (source_id, club_id, valid_from, valid_to, note)"
        " VALUES (?,?,?,?,?)",
        [(s.source_id, s.club_id, s.valid_from, s.valid_to, s.note) for s in source_ids])
    try:
        yield con
    finally:
        con.close()


def test_seed_satisfies_schema_constraints(seeded):
    """CHECK / UNIQUE / FK をすべて通ること。"""
    assert seeded.execute("SELECT COUNT(*) FROM seasons").fetchone()[0] == EXPECTED_SEASONS
    assert seeded.execute("SELECT COUNT(*) FROM clubs").fetchone()[0] == EXPECTED_CLUBS
    assert seeded.execute(
        "SELECT COUNT(*) FROM club_source_ids").fetchone()[0] == EXPECTED_CLUBS


def test_every_source_id_resolves_to_a_club(seeded):
    """**工程2の完了条件。** 公式IDがすべて `club_id` に解決すること。"""
    unresolved = seeded.execute(
        "SELECT k.source_id FROM club_source_ids k"
        " LEFT JOIN clubs c ON c.id = k.club_id WHERE c.id IS NULL"
    ).fetchall()
    assert unresolved == []
    assert seeded.execute(
        "SELECT COUNT(*) FROM club_source_ids k JOIN clubs c ON c.id = k.club_id"
    ).fetchone()[0] == EXPECTED_CLUBS


def test_old_b1_and_new_league_share_one_club_row(seeded):
    """**旧B1と新リーグのクラブが同一行に紐付くこと。**

    703 は 2016-17 で栃木ブレックス、2026-27 で宇都宮ブレックスだが、
    `clubs` では1行である。分断されると旧B1の Elo を引き継げない。
    """
    rows = seeded.execute(
        "SELECT c.id, c.name FROM club_source_ids k JOIN clubs c ON c.id = k.club_id"
        " WHERE k.source_id = '703'").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "703"


def test_reapplying_seed_violates_unique(seeded):
    """同じ行を素の INSERT で二度入れると弾かれること。

    冪等性はサーバ側の `ON CONFLICT DO UPDATE` が担保する（詳細設計 3.4）。
    ここでは主キーが実際に効いていることを確認する。
    """
    with pytest.raises(sqlite3.IntegrityError):
        seeded.execute("INSERT INTO clubs (id, slug, name) VALUES ('703','x','y')")


# --- 送信ペイロード -------------------------------------------------------------


def test_payload_uses_named_arrays():
    """ボディが名前付き配列であること（テーブル名を引数に取らない）。"""
    payload = build_payload(*load_all())
    assert set(payload) == {"seasons", "clubs", "clubSourceIds"}
    assert "table" not in payload and "tableName" not in payload
    assert set(payload["seasons"][0]) == {"id", "label", "league", "startDate", "endDate"}
    assert set(payload["clubs"][0]) == {"id", "slug", "name"}
    assert set(payload["clubSourceIds"][0]) == {
        "sourceId", "clubId", "validFrom", "validTo", "note"}


def test_payload_fits_one_request():
    """1リクエスト（50クエリ以内）に収まること（詳細設計 3.4）。"""
    payload = build_payload(*load_all())
    cols = {"seasons": 5, "clubs": 5, "clubSourceIds": 5}
    statements = sum(
        -(-len(payload[k]) // (100 // cols[k])) for k in payload)     # 切り上げ
    assert statements <= 40, statements


# --- 異常系（握りつぶさずに落ちること） -----------------------------------------


def _write(tmp_path: pathlib.Path, name: str, header: list[str], rows: list[list[str]]):
    with (tmp_path / name).open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def test_missing_file_raises(tmp_path):
    with pytest.raises(SeedError):
        load_seasons(tmp_path)


def test_bad_league_raises(tmp_path):
    _write(tmp_path, "seasons.csv",
           ["id", "label", "league", "start_date", "end_date"],
           [["2026-27-X", "2026-27", "X", "2026-09-22", "2027-05-30"]])
    with pytest.raises(SeedError):
        load_seasons(tmp_path)


def test_bad_slug_raises(tmp_path):
    _write(tmp_path, "clubs.csv", ["id", "slug", "name"],
           [["703", "Utsunomiya_Brex", "宇都宮ブレックス"]])
    with pytest.raises(SeedError):
        load_clubs(tmp_path)


def test_duplicate_slug_raises(tmp_path):
    _write(tmp_path, "clubs.csv", ["id", "slug", "name"],
           [["703", "dup", "架空A"], ["704", "dup", "架空B"]])
    with pytest.raises(SeedError):
        load_clubs(tmp_path)


def test_reversed_season_range_raises(tmp_path):
    _write(tmp_path, "seasons.csv",
           ["id", "label", "league", "start_date", "end_date"],
           [["2026-27-PREMIER", "2026-27", "PREMIER", "2027-05-30", "2026-09-22"]])
    with pytest.raises(SeedError):
        load_seasons(tmp_path)
