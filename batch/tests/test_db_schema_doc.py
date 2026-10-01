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


# --- ER図（Mermaid） ---

def er_blocks() -> list[str]:
    body = DOC.read_text(encoding="utf-8")
    return [b.split("```", 1)[0] for b in body.split("```mermaid\n")[1:]]


def test_three_diagrams_are_generated() -> None:
    """**24表を1枚にしない。** 分類ごとに3枚（詳細設計 1章の分類）。"""
    assert len(er_blocks()) == 3


@pytest.mark.parametrize("index", range(3))
def test_every_diagram_declares_itself_as_er(index: int) -> None:
    assert er_blocks()[index].splitlines()[0].strip() == "erDiagram"


def test_every_entity_in_the_diagrams_is_a_real_table() -> None:
    """**図に実在しない表を書かない。** 関連は FK 定義そのものである。"""
    tables = set(schema())
    unknown: set[str] = set()
    for block in er_blocks():
        for line in block.splitlines()[1:]:
            parts = line.strip().split()
            if not parts:
                continue
            if len(parts) == 1:  # FK を持たない表（名前だけの宣言）
                unknown |= {parts[0]} - tables
            else:
                unknown |= {parts[0], parts[2]} - tables
    assert not unknown, f"図に実在しない表がある: {sorted(unknown)}"


def test_relationship_markers_are_valid() -> None:
    """多重度の記号が Mermaid の語彙であること。**構文が壊れると図が出ない。**"""
    valid = {"||", "|o", "o{", "}o", "|{", "}|"}
    for block in er_blocks():
        for line in block.splitlines()[1:]:
            parts = line.strip().split()
            if len(parts) < 3:
                continue
            left, _, right = parts[1].partition("--")
            assert left in valid and right in valid, f"記号が不正: {line.strip()}"


def test_one_to_one_child_side_is_optional() -> None:
    """**1:1 の子側は `||` ではなく `|o`。** 親に子が無いこともある
    （`predictions` に対応する `prediction_results` は照合前には存在しない）。"""
    for block in er_blocks():
        for line in block.splitlines():
            assert "--||" not in line, f"子側が「必ず1件」になっている: {line.strip()}"


def test_self_reference_is_listed_outside_the_diagram() -> None:
    """自己参照は図に入れない（1箇所で図全体が出なくなる事故を避ける）。

    代わりに表で示す。`games.rescheduled_to` が実在する唯一の自己参照である。
    """
    body = DOC.read_text(encoding="utf-8")
    assert "自己参照" in body
    assert "`rescheduled_to`" in body
    for block in er_blocks():
        for line in block.splitlines()[1:]:
            parts = line.strip().split()
            if len(parts) >= 3:
                assert parts[0] != parts[2], f"自己参照が図に入っている: {line.strip()}"
