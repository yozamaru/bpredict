"""特徴量生成の入力。**D1 を読まない**（CLAUDE.md 絶対ルール3）。

入力は `batch/snapshot/*.parquet` だけである。読み込み前に `MANIFEST.json` の
行数と SHA256 を照合し、不一致なら処理を中止する（基本設計 2.2）。

このモジュールは HTTP クライアントも D1 クライアントも import しない。
`test_training_reads_no_d1` がそれを固定する。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

# 基本設計 2.2 のスナップショット構成。順序は FK の依存順（再投入で使う）。
SNAPSHOT_TABLES = (
    "seasons",
    "clubs",
    "club_seasons",
    "venues",
    "venue_revisions",
    "players",
    "player_seasons",
    "games",
    "team_games",
    "team_game_stats",
    "player_game_stats",
    "game_entries",
    "team_ratings",
)

MANIFEST_NAME = "MANIFEST.json"


class SnapshotError(RuntimeError):
    """スナップショットが壊れている、または MANIFEST と一致しない。"""


@dataclass(frozen=True)
class Dataset:
    """メモリ内データセット。全量をロードして結合・フィルタする。"""

    tables: dict[str, pd.DataFrame]

    def table(self, name: str) -> pd.DataFrame:
        if name not in self.tables:
            raise SnapshotError(f"スナップショットに {name} がない")
        return self.tables[name]

    @property
    def max_finished_at(self) -> str | None:
        """最新試合の終了時刻。`predictions.data_as_of` に記録する（要件 6.3）。"""
        finished = self.table("games")["finished_at"].dropna()
        return None if finished.empty else str(finished.max())


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def export_sqlite(connection: sqlite3.Connection) -> Dataset:
    """SQLite からデータセットを組み立てる。

    **これは D1 を入力にする経路ではない。** 使うのはテスト（in-memory）と
    スナップショットの再生成（`scripts/rebuild_snapshot.py`。復旧手段であって
    日常の学習経路ではない。基本設計 2.2）に限る。
    """
    tables: dict[str, pd.DataFrame] = {}
    for name in SNAPSHOT_TABLES:
        cursor = connection.execute(f"SELECT * FROM {name}")
        columns = [description[0] for description in cursor.description]
        tables[name] = pd.DataFrame(cursor.fetchall(), columns=columns)
    return Dataset(tables=tables)


def write_snapshot(
    dataset: Dataset,
    directory: Path,
    *,
    tables: Sequence[str] | None = None,
) -> dict[str, object]:
    """Parquet と MANIFEST を書き出す。D1 へ書いたのと同じデータから呼ぶ。

    `tables` を渡すと**そのテーブルだけを書き直す**（`daily_ingest` は
    ファクトを書いた後に `team_ratings` だけを上書きする。基本設計 2.2）。
    スナップショットはリポジトリにコミットするため、変わっていないファイルを
    毎回書き換えると差分が無意味に膨らむ。

    **MANIFEST は必ずディスクの現物から作り直す。** 書き直さなかったテーブルの
    エントリを前回の MANIFEST から引き写すと、ファイルが別経路で変わったときに
    MANIFEST だけが正しく見え、`verify_snapshot()` が検知できなくなる。
    """
    directory.mkdir(parents=True, exist_ok=True)
    targets = SNAPSHOT_TABLES if tables is None else tuple(tables)
    unknown = sorted(set(targets) - set(SNAPSHOT_TABLES))
    if unknown:
        raise SnapshotError(f"スナップショットの構成にないテーブル: {unknown}")
    for name in targets:
        dataset.table(name).to_parquet(directory / f"{name}.parquet", index=False)

    files: dict[str, dict[str, object]] = {}
    for name in SNAPSHOT_TABLES:
        path = directory / f"{name}.parquet"
        if not path.exists():
            raise SnapshotError(f"{name}.parquet がない（部分書き出しの前提が崩れている）")
        files[name] = {"rows": len(pd.read_parquet(path)), "sha256": _digest(path)}
    manifest: dict[str, object] = {
        "version": 1,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "max_finished_at": dataset.max_finished_at,
        "files": files,
    }
    (directory / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def verify_snapshot(directory: Path) -> dict[str, object]:
    """MANIFEST の行数と SHA256 を照合する。読み込みの前に必ず通す。"""
    manifest_path = directory / MANIFEST_NAME
    if not manifest_path.exists():
        raise SnapshotError("MANIFEST.json がない")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError:
        raise SnapshotError("MANIFEST.json が不正") from None
    if not isinstance(manifest, dict):
        raise SnapshotError("MANIFEST.json のルートがオブジェクトでない")
    checked: dict[str, object] = manifest
    files = checked.get("files")
    if not isinstance(files, dict) or set(files) != set(SNAPSHOT_TABLES):
        raise SnapshotError("MANIFEST の対象テーブルが構成と一致しない")
    for name, expected in files.items():
        path = directory / f"{name}.parquet"
        if not path.exists():
            raise SnapshotError(f"{name}.parquet がない")
        if _digest(path) != expected.get("sha256"):
            raise SnapshotError(f"{name}.parquet の SHA256 が MANIFEST と一致しない")
        if len(pd.read_parquet(path)) != expected.get("rows"):
            raise SnapshotError(f"{name}.parquet の行数が MANIFEST と一致しない")
    return checked


def load_snapshot(directory: Path) -> Dataset:
    """スナップショットを読む。**特徴量・学習・推論の唯一の入力。**"""
    verify_snapshot(directory)
    tables = {
        name: pd.read_parquet(directory / f"{name}.parquet") for name in SNAPSHOT_TABLES
    }
    return Dataset(tables=tables)
