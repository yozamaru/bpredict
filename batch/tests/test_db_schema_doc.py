"""`docs/db-schema.md` が古くなっていないことを検査する。

**生成物なので手で直さない**（CLAUDE.md「生成物を手で編集しない」）。ここで見るのは
「マイグレーションにある表と列が、文書に現れているか」だけである。

**行数は検査しない。** 行数は D1 の実データを測った値で、取り込みのたびに変わる。
これを検査すると、データが増えるたびに CI が落ちる。
"""
from __future__ import annotations

import pathlib
import sqlite3

import pytest

from batch.tests.conftest import apply_migrations

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DOC = REPO_ROOT / "docs" / "db-schema.md"


def schema() -> dict[str, list[str]]:
    con = sqlite3.connect(":memory:")
    apply_migrations(con)
    tables = [
        str(r[0]) for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'",
        )
    ]
    return {
        t: [str(r[1]) for r in con.execute(f"PRAGMA table_info({t})")] for t in tables
    }


def test_doc_exists() -> None:
    assert DOC.exists(), "docs/db-schema.md がない。scripts/describe_schema.py で生成する"


@pytest.mark.parametrize("table", sorted(schema()))
def test_every_table_appears_in_the_doc(table: str) -> None:
    """表を足して文書を作り直し忘れると、ここで落ちる。"""
    assert f"### {table}\n" in DOC.read_text(encoding="utf-8"), (
        f"{table} が docs/db-schema.md にない。"
        "`python3 scripts/describe_schema.py --database <sqlite>` で作り直す"
    )


def test_every_column_appears_in_the_doc() -> None:
    """列を足した場合も同じ。**どの表のどの列が抜けたかを出す。**"""
    body = DOC.read_text(encoding="utf-8")
    missing = [
        f"{table}.{column}"
        for table, columns in schema().items()
        for column in columns
        if f"| `{column}`" not in body.split(f"### {table}\n", 1)[-1].split("\n### ", 1)[0]
    ]
    assert not missing, (
        f"docs/db-schema.md に現れない列がある: {missing}。"
        "`python3 scripts/describe_schema.py --database <sqlite>` で作り直す"
    )


def test_doc_says_it_is_generated() -> None:
    """**手で直す人を止める。** 生成物であることが冒頭に書いてあること。"""
    head = DOC.read_text(encoding="utf-8")[:600]
    assert "scripts/describe_schema.py" in head
    assert "手で編集しない" in head


def test_doc_points_at_the_design_for_definitions() -> None:
    """定義と理由は詳細設計1章にある。**二重に書かない**ことを文書自身が宣言する。"""
    head = DOC.read_text(encoding="utf-8")[:900]
    assert "design-detail.md" in head
