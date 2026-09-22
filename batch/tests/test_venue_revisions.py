"""会場の名称・収容人数の履歴（詳細設計 1.2 / 4.9）の検証。

**この表は派生である。** 名称の唯一の入力は `games.venue_name_at_game`（その試合時点の
`StadiumNameJ`）で、`venues.name` は初出の名称で固定される。列を足す前は3つ組が
取り込みの瞬間にしか存在せず、後から履歴を作れなかった。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from batch.features.dataset import export_sqlite, load_snapshot, write_snapshot
from batch.jobs import build_venue_revisions
from batch.loader.api import InternalApi, Response
from batch.masters.venue_revisions import (
    OPEN_ENDED,
    VenueRevisionError,
    build,
    build_name_periods,
    load_capacities,
)


def _games(rows: list[tuple[str, str, str | None, str]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"venue_id": venue, "game_date": date, "venue_name_at_game": name,
             "season_id": season}
            for venue, date, name, season in rows
        ]
    )


# --- 名称の区間 -----------------------------------------------------------

def test_single_name_becomes_one_open_ended_period():
    result = build_name_periods(_games([
        ("v1", "2016-10-01", "アリーナA", "2016-17"),
        ("v1", "2017-01-15", "アリーナA", "2016-17"),
    ]))
    assert [(r.valid_from, r.valid_to, r.name) for r in result.revisions] == [
        ("2016-10-01", OPEN_ENDED, "アリーナA"),
    ]


def test_rename_closes_the_previous_period_on_the_day_before():
    """直前の区間の `valid_to` は、改称後の最初の試合の**前日**（詳細設計 1.2）。"""
    result = build_name_periods(_games([
        ("v1", "2016-10-01", "旧アリーナ", "2016-17"),
        ("v1", "2017-10-07", "新アリーナ", "2017-18"),
    ]))
    assert [(r.valid_from, r.valid_to, r.name) for r in result.revisions] == [
        ("2016-10-01", "2017-10-06", "旧アリーナ"),
        ("2017-10-07", OPEN_ENDED, "新アリーナ"),
    ]


def test_periods_are_built_in_game_date_order_not_row_order():
    """入力の行順に依存しないこと（洗い替えが冪等であるための前提）。"""
    rows = [
        ("v1", "2017-10-07", "新アリーナ", "2017-18"),
        ("v1", "2016-10-01", "旧アリーナ", "2016-17"),
    ]
    forward = build_name_periods(_games(rows)).revisions
    backward = build_name_periods(_games(list(reversed(rows)))).revisions
    assert forward == backward
    assert [r.name for r in forward] == ["旧アリーナ", "新アリーナ"]


def test_name_restored_later_opens_a_third_period():
    """元の名称に戻った場合、区間を3つにする（同じ名称でも期間は別）。"""
    result = build_name_periods(_games([
        ("v1", "2016-10-01", "A", "2016-17"),
        ("v1", "2017-10-07", "B", "2017-18"),
        ("v1", "2018-10-05", "A", "2018-19"),
    ]))
    assert [(r.valid_from, r.name) for r in result.revisions] == [
        ("2016-10-01", "A"), ("2017-10-07", "B"), ("2018-10-05", "A"),
    ]


def test_null_name_does_not_open_a_period():
    """`venue_name_at_game` が NULL の試合は区間の判定に使わない。

    欠損を「名称が変わった」と読まない（0 と欠損を区別するのと同じ理由）。
    """
    result = build_name_periods(_games([
        ("v1", "2016-10-01", "アリーナA", "2016-17"),
        ("v1", "2016-11-01", None, "2016-17"),
        ("v1", "2016-12-01", "アリーナA", "2016-17"),
    ]))
    assert len(result.revisions) == 1
    assert result.fallback_venue_ids == []


def test_empty_string_name_is_treated_as_missing():
    """空文字も欠損として扱う（サイトが 0 ではなく空文字を返す実例がある）。"""
    result = build_name_periods(_games([("v1", "2016-10-01", "", "2016-17")]))
    assert result.revisions == []
    assert result.fallback_venue_ids == ["v1"]


def test_venue_without_any_name_falls_back_and_is_reported():
    """区間が1つもない会場は `venues.name` へのフォールバックとして報告する。

    黙ってフォールバックすると、過去試合が現在名で表示されていることに気づけない。
    """
    result = build_name_periods(_games([
        ("v1", "2016-10-01", None, "2016-17"),
        ("v2", "2016-10-01", "アリーナB", "2016-17"),
    ]))
    assert result.fallback_venue_ids == ["v1"]
    assert {r.venue_id for r in result.revisions} == {"v2"}


def test_intra_season_rename_is_reported_but_does_not_stop():
    """同一シーズン内の名称変更は表記ゆれの疑いとして報告する（名寄せしない）。

    命名権の変更はシーズン境界で起きるのが通例であり、シーズン内の変更は全角・半角や
    「市立」の有無といった表記ゆれの疑いがある。名寄せの規則を推測で作らない。
    """
    result = build_name_periods(_games([
        ("v1", "2016-10-01", "アリーナ", "2016-17"),
        ("v1", "2016-12-01", "ア リ ー ナ", "2016-17"),
    ]))
    assert len(result.revisions) == 2          # 止めない
    assert len(result.intra_season_changes) == 1
    assert "v1" in result.intra_season_changes[0]


def test_season_boundary_rename_is_not_reported():
    """シーズンをまたぐ改称は通常の事象なので報告しない（陰性確認）。"""
    result = build_name_periods(_games([
        ("v1", "2016-10-01", "旧", "2016-17"),
        ("v1", "2017-10-07", "新", "2017-18"),
    ]))
    assert result.intra_season_changes == []


def test_games_without_venue_are_skipped():
    """`venue_id` が NULL の試合は会場の履歴に関与しない。"""
    frame = _games([("v1", "2016-10-01", "A", "2016-17")])
    frame.loc[len(frame)] = {"venue_id": None, "game_date": "2016-10-02",
                             "venue_name_at_game": "B", "season_id": "2016-17"}
    result = build_name_periods(frame)
    assert {r.venue_id for r in result.revisions} == {"v1"}


def test_missing_column_raises():
    with pytest.raises(VenueRevisionError):
        build_name_periods(pd.DataFrame([{"venue_id": "v1", "game_date": "2016-10-01"}]))


# --- 収容人数の突き合わせ -------------------------------------------------

def test_capacity_is_matched_by_venue_and_valid_from():
    result = build(
        _games([
            ("v1", "2016-10-01", "旧", "2016-17"),
            ("v1", "2017-10-07", "新", "2017-18"),
        ]),
        capacities={("v1", "2017-10-07"): 5200},
    )
    by_start = {r.valid_from: r.capacity for r in result.revisions}
    assert by_start == {"2016-10-01": None, "2017-10-07": 5200}


def test_capacity_row_that_matches_nothing_raises():
    """名称区間に当たらない収容人数の行は**中止**する（詳細設計 4.9）。

    収容人数だけのために名称区間を勝手に分割しない。CSV の `valid_from` を区間の
    開始日に合わせるのは運営者の判断である。
    """
    with pytest.raises(VenueRevisionError, match="一致しない"):
        build(
            _games([("v1", "2016-10-01", "A", "2016-17")]),
            capacities={("v1", "2018-01-01"): 5000},
        )


def test_no_capacity_csv_leaves_every_capacity_null():
    result = build(_games([("v1", "2016-10-01", "A", "2016-17")]), capacities={})
    assert [r.capacity for r in result.revisions] == [None]


def test_capacity_csv_requires_the_documented_columns(tmp_path: Path):
    path = tmp_path / "venue_revisions.csv"
    path.write_text("venue_id,valid_from\nv1,2016-10-01\n", encoding="utf-8")
    with pytest.raises(VenueRevisionError, match="列がない"):
        load_capacities(path)


def test_capacity_csv_rejects_duplicate_keys(tmp_path: Path):
    path = tmp_path / "venue_revisions.csv"
    path.write_text(
        "venue_id,valid_from,capacity,source\n"
        "v1,2016-10-01,5000,https://example.invalid/a\n"
        "v1,2016-10-01,5100,https://example.invalid/b\n",
        encoding="utf-8",
    )
    with pytest.raises(VenueRevisionError, match="重複"):
        load_capacities(path)


def test_capacity_csv_accepts_blank_as_null(tmp_path: Path):
    path = tmp_path / "venue_revisions.csv"
    path.write_text(
        "venue_id,valid_from,capacity,source\nv1,2016-10-01,,未確認\n", encoding="utf-8"
    )
    assert load_capacities(path) == {("v1", "2016-10-01"): None}


def test_missing_capacity_csv_is_not_an_error(tmp_path: Path):
    assert load_capacities(tmp_path / "does-not-exist.csv") == {}


# --- ジョブ ---------------------------------------------------------------

class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, method, body, headers):
        import json

        payload = json.loads(body) if body else {}
        self.calls.append((url, payload))
        return Response(200, json.dumps({"data": {"applied": {}}}))


def _api(recorder: _Recorder) -> InternalApi:
    return InternalApi("https://example.invalid", "t", transport=recorder)


def test_run_derives_from_the_seeded_games(
    seeded_db: sqlite3.Connection, tmp_path: Path
) -> None:
    """シードの改称（1会場が2シーズン目に改称）が区間2つになること。"""
    write_snapshot(export_sqlite(seeded_db), tmp_path)
    recorder = _Recorder()
    result = build_venue_revisions.run(api=_api(recorder), snapshot_dir=tmp_path)

    per_venue: dict[str, int] = {}
    for revision in result.revisions:
        per_venue[revision.venue_id] = per_venue.get(revision.venue_id, 0) + 1
    renamed = [venue for venue, n in per_venue.items() if n == 2]
    assert len(renamed) == 1, f"改称した会場が1つであること: {per_venue}"
    assert all(n in (1, 2) for n in per_venue.values())
    assert result.fallback_venue_ids == []
    assert result.intra_season_changes == []


def test_run_writes_snapshot_and_posts_once(
    seeded_db: sqlite3.Connection, tmp_path: Path
) -> None:
    """スナップショットを D1 より先に書き、**1リクエストで送る**こと。

    会場を2リクエストに分けると、期間 DELETE が前のリクエストの行を消す。
    """
    write_snapshot(export_sqlite(seeded_db), tmp_path)
    recorder = _Recorder()
    result = build_venue_revisions.run(api=_api(recorder), snapshot_dir=tmp_path)

    assert len(recorder.calls) == 1
    _, payload = recorder.calls[0]
    assert len(payload["revisions"]) == len(result.revisions)
    assert payload["fromDate"] <= payload["toDate"]
    # MANIFEST が書き直されており、そのまま読み直せる
    stored = load_snapshot(tmp_path).table("venue_revisions")
    assert len(stored) == len(result.revisions)


def test_run_is_idempotent(seeded_db: sqlite3.Connection, tmp_path: Path) -> None:
    """2回流して同じ結果になること（派生テーブルの洗い替え）。"""
    write_snapshot(export_sqlite(seeded_db), tmp_path)
    first, second = _Recorder(), _Recorder()
    a = build_venue_revisions.run(api=_api(first), snapshot_dir=tmp_path)
    digest = (tmp_path / "venue_revisions.parquet").read_bytes()
    b = build_venue_revisions.run(api=_api(second), snapshot_dir=tmp_path)
    assert a.revisions == b.revisions
    assert digest == (tmp_path / "venue_revisions.parquet").read_bytes()
    assert [p for _, p in first.calls] == [p for _, p in second.calls]


def test_run_refuses_to_split_across_requests(monkeypatch, seeded_db, tmp_path) -> None:
    """上限を超えたら分割せず失敗すること（黙って分けると履歴が壊れる）。"""
    write_snapshot(export_sqlite(seeded_db), tmp_path)
    monkeypatch.setattr(build_venue_revisions, "ROWS_PER_REQUEST", 1)
    with pytest.raises(VenueRevisionError, match="上限"):
        build_venue_revisions.run(api=_api(_Recorder()), snapshot_dir=tmp_path)
