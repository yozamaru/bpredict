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


# --- コミット済みのスナップショットが、マスタの CSV を反映していること ---
#
# **2026-10-03 に、座標140件が D1 にだけ入っていてスナップショットは 0件だった。**
# スナップショットは特徴量・学習・推論の唯一の入力であり（絶対ルール3）、D1 は
# 公開APIの表示用の複製である。したがって #16（移動距離）は「座標がない」として
# 全件欠損し、#15 も同じ道をたどるところだった。
#
# **`test_snapshot_matches_d1` では捕まらない。** あれは行数と主キー集合を見る仕様で、
# **列の値が古いことは見ない**（しかも、まだ実装されていない）。ここは D1 を要しない。

REPO = Path(__file__).resolve().parents[2]
COMMITTED_SNAPSHOT = REPO / "batch" / "snapshot"


def _committed() -> dataset.Dataset:
    if not (COMMITTED_SNAPSHOT / MANIFEST_NAME).exists():
        pytest.skip("コミット済みのスナップショットがない")
    return load_snapshot(COMMITTED_SNAPSHOT)


def test_snapshot_reflects_the_venue_geo_csv() -> None:
    """`venues_geo.csv` に座標がある会場は、スナップショットにも座標があること。"""
    from batch.jobs import resolve_venue_geo

    csv_path = REPO / resolve_venue_geo.CSV_PATH
    if not csv_path.exists():
        pytest.skip("venues_geo.csv がない")
    rows = resolve_venue_geo.read_csv(csv_path)
    expected = {v for v, row in rows.items() if row.get("lat") and row.get("lng")}
    if not expected:
        pytest.skip("CSV に座標の行がない")

    venues = _committed().table("venues")
    known = set(venues["id"].astype(str))
    have = {
        str(r["id"]) for r in venues.to_dict("records")
        if r.get("lat") is not None and r.get("lng") is not None
        and not pd_isna(r.get("lat")) and not pd_isna(r.get("lng"))
    }
    missing = sorted((expected & known) - have)
    assert not missing, (
        f"CSV に座標があるのにスナップショットに無い会場: {missing[:10]}"
        "（`python -m batch.jobs.resolve_venue_geo --sync-snapshot` を流してコミットする）"
    )


def test_snapshot_reflects_the_primary_venue_csv() -> None:
    """`club_primary_venues.csv` の本拠会場が、スナップショットにも入っていること。"""
    from batch.masters import primary_venues

    csv_path = REPO / primary_venues.PRIMARY_VENUE_CSV
    if not csv_path.exists():
        pytest.skip("club_primary_venues.csv がない")
    rows = primary_venues.load_csv(csv_path)
    if not rows:
        pytest.skip("CSV が空である")
    primary = primary_venues.primary_of(rows)

    seasons = _committed().table("club_seasons")
    missing = [
        (str(r["season_id"]), str(r["club_id"]))
        for r in seasons.to_dict("records")
        if (str(r["season_id"]), str(r["club_id"])) in primary
        and (r.get("primary_venue_id") is None or pd_isna(r.get("primary_venue_id")))
    ]
    assert not missing, (
        f"CSV に本拠会場があるのにスナップショットが NULL: {missing[:10]}"
        "（`python -m batch.jobs.derive_primary_venues --sync-snapshot` を流してコミットする）"
    )


def pd_isna(value: object) -> bool:
    """欠損の判定。`pandas.isna` は配列も受けるため、ここでは単値に限る。"""
    import math

    if value is None:
        return True
    if isinstance(value, float):
        return math.isnan(value)
    return str(value) in ("nan", "NaT", "<NA>", "")
