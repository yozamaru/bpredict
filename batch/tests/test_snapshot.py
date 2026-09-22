"""スナップショットの整合性と、学習経路が D1 を読まないことの検証（基本設計 8.3）。"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

from batch.features import base, builder, constants, dataset, player, schedule_ctx, team_strength
from batch.features.dataset import (
    MANIFEST_NAME,
    SNAPSHOT_TABLES,
    SnapshotError,
    export_sqlite,
    load_snapshot,
    verify_snapshot,
    write_snapshot,
)

# 各テーブルの主キー（DDL と対応）。行数だけでなく集合の一致も見る。
PRIMARY_KEYS = {
    "seasons": ("id",),
    "clubs": ("id",),
    "club_seasons": ("club_id", "season_id"),
    "venues": ("id",),
    "venue_revisions": ("venue_id", "valid_from"),
    "players": ("id",),
    "player_seasons": ("player_id", "season_id", "club_id"),
    "games": ("id",),
    "team_games": ("club_id", "game_date", "game_id"),
    "team_game_stats": ("game_id", "club_id"),
    "player_game_stats": ("game_id", "player_id"),
    "game_entries": ("game_id", "player_id"),
    "team_ratings": ("club_id", "as_of_date"),
}


def test_snapshot_matches_source(seeded_db: sqlite3.Connection, tmp_path: Path) -> None:
    """書き出したスナップショットの行数と主キー集合が元と一致すること。"""
    source = export_sqlite(seeded_db)
    write_snapshot(source, tmp_path)
    loaded = load_snapshot(tmp_path)

    assert set(loaded.tables) == set(SNAPSHOT_TABLES)
    for name, keys in PRIMARY_KEYS.items():
        original, restored = source.table(name), loaded.table(name)
        assert len(original) == len(restored), name
        assert {tuple(row) for row in original[list(keys)].itertuples(index=False)} == {
            tuple(row) for row in restored[list(keys)].itertuples(index=False)
        }, name


def test_snapshot_manifest_hashes(seeded_db: sqlite3.Connection, tmp_path: Path) -> None:
    """MANIFEST の SHA256 が実ファイルと一致しなければ中止すること。"""
    write_snapshot(export_sqlite(seeded_db), tmp_path)
    verify_snapshot(tmp_path)  # 改変前は通る

    target = tmp_path / "games.parquet"
    target.write_bytes(target.read_bytes() + b"\x00")
    with pytest.raises(SnapshotError):
        verify_snapshot(tmp_path)


def test_snapshot_manifest_row_count(seeded_db: sqlite3.Connection, tmp_path: Path) -> None:
    """行数が MANIFEST と一致しなければ中止すること。"""
    manifest = write_snapshot(export_sqlite(seeded_db), tmp_path)
    files = manifest["files"]
    assert isinstance(files, dict)
    files["games"]["rows"] = int(files["games"]["rows"]) + 1
    (tmp_path / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(SnapshotError):
        verify_snapshot(tmp_path)


@pytest.mark.parametrize(
    "prepare",
    [
        pytest.param(lambda path: None, id="manifest-missing"),
        pytest.param(
            lambda path: (path / MANIFEST_NAME).write_text("{", encoding="utf-8"),
            id="manifest-broken",
        ),
        pytest.param(
            lambda path: (path / MANIFEST_NAME).write_text(
                json.dumps({"files": {"games": {"rows": 0, "sha256": "x"}}}), encoding="utf-8"
            ),
            id="manifest-partial",
        ),
    ],
)
def test_snapshot_requires_a_valid_manifest(tmp_path: Path, prepare) -> None:
    """MANIFEST がない・壊れている・構成が違う場合は読み込まない。"""
    prepare(tmp_path)
    with pytest.raises(SnapshotError):
        load_snapshot(tmp_path)


def test_max_finished_at_is_the_latest_finished_game(seeded_db: sqlite3.Connection) -> None:
    """`data_as_of` に記録する値が最新試合の終了時刻であること（要件 6.3）。"""
    source = export_sqlite(seeded_db)
    expected = seeded_db.execute("SELECT MAX(finished_at) FROM games").fetchone()[0]
    assert source.max_finished_at == expected


def test_training_reads_no_d1(seeded_db: sqlite3.Connection, monkeypatch) -> None:
    """特徴量生成の経路で D1・HTTP に一切触れないこと（CLAUDE.md 絶対ルール3）。

    入力はスナップショットのみである。逐次クエリ方式では `games` の全件走査を
    試合ごとに繰り返し、1日150万〜1,450万行（上限500万行per日）に達する。
    """
    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("特徴量生成が HTTP を呼んだ")

    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    monkeypatch.setattr("urllib.request.OpenerDirector.open", forbidden)

    source = export_sqlite(seeded_db)
    row = seeded_db.execute(
        "SELECT id, tipoff_at FROM games ORDER BY game_date DESC LIMIT 1"
    ).fetchone()
    as_of = datetime.fromisoformat(str(row[1]))
    assert builder.build_features(str(row[0]), as_of, source)


def test_feature_modules_do_not_import_clients() -> None:
    """特徴量のモジュールが取り込み系・HTTP のモジュールを import しないこと。

    実行時に呼ばないだけでは足りない。import してあると、後から「ついでに
    1行だけ読む」実装が入る余地が残る。
    """
    forbidden = {"batch.scraper.client", "batch.jobs.seed_master", "urllib.request", "http.client"}
    for module in (base, builder, constants, dataset, player, schedule_ctx, team_strength):
        imported = {
            name
            for name, value in vars(module).items()
            if getattr(value, "__name__", "") in forbidden
        }
        assert not imported, f"{module.__name__} が {imported} を import している"
        assert module.__name__ in sys.modules
