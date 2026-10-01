"""`docs/db-schema.md` が古くなっていないことを検査する。

**生成物なので手で直さない**（CLAUDE.md「生成物を手で編集しない」）。ここで見るのは
「マイグレーションにある表と列が、文書に現れているか」だけである。

**行数は検査しない。** 行数は D1 の実データを測った値で、取り込みのたびに変わる。
これを検査すると、データが増えるたびに CI が落ちる。
"""
from __future__ import annotations

import pathlib
import sqlite3
from typing import Any

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


# --- 表と列の説明（`scripts/schema_notes.py`） ---

def notes_modules() -> tuple[Any, Any]:
    """`scripts/` は パッケージでないため、パスを足して読む。"""
    import sys
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import describe_schema
    import schema_notes
    return describe_schema, schema_notes


def test_every_table_has_a_role() -> None:
    """**表を足したら役割を書く。** 書かないと一覧の意味が落ちる。"""
    _, notes = notes_modules()
    missing = sorted(set(schema()) - set(notes.TABLES))
    assert not missing, (
        f"役割が未記載の表: {missing}。scripts/schema_notes.py の TABLES に足す"
    )


def test_notes_have_no_entry_for_a_table_that_does_not_exist() -> None:
    """**消えた表の説明を残さない。** 残ると実在しない表の話が文書に出る。"""
    _, notes = notes_modules()
    stale = sorted(set(notes.TABLES) - set(schema()))
    assert not stale, f"実在しない表の説明がある: {stale}"


def test_every_column_has_a_description() -> None:
    """DDL コメント・注記・FK 由来・接頭辞からの導出 のいずれかが当たること。

    **列を足したら説明を書く**という強制になる。書けないなら、それは設計が
    その列を定義していないという合図である（推測で埋めない）。
    """
    describe_schema, _ = notes_modules()
    con = describe_schema.structure()
    blank = [
        f"{t}.{c.name}"
        for t in sorted(schema())
        for c in describe_schema.columns_of(con, t)
        if not c.description
    ]
    assert not blank, (
        f"説明のない列: {blank}。scripts/schema_notes.py に足す"
        "（設計に定義がないなら、まず設計を直す）"
    )


def test_stale_column_notes_are_not_left_behind() -> None:
    """**消えた列の説明を残さない。**"""
    _, notes = notes_modules()
    cols = schema()
    stale = [
        f"{t}.{c}" for (t, c) in notes.SPECIFIC
        if t not in cols or c not in cols[t]
    ]
    assert not stale, f"実在しない列の説明がある: {stale}"


def test_allowed_values_come_from_the_check_constraints() -> None:
    """許容値は DDL の CHECK から読む。**手で並べない。**"""
    describe_schema, _ = notes_modules()
    con = describe_schema.structure()
    found = {
        c.name: c.allowed
        for c in describe_schema.columns_of(con, "games")
        if c.allowed
    }
    assert found["status"] == ("SCHEDULED", "FINISHED", "POSTPONED", "CANCELLED")
    assert found["competition"] == ("REGULAR", "PLAYOFF")


def test_source_fields_are_only_on_the_box_score_tables() -> None:
    """取得元のフィールド名は、ボックススコアの表にだけ付ける。"""
    describe_schema, notes = notes_modules()
    con = describe_schema.structure()
    for table in sorted(schema()):
        for c in describe_schema.columns_of(con, table):
            if c.source:
                assert table in notes.SOURCE_FIELD_TABLES, f"{table}.{c.name}"


def test_the_doc_shows_descriptions_and_allowed_values() -> None:
    body = DOC.read_text(encoding="utf-8")
    assert "許容値: `SCHEDULED`" in body
    assert "取得元: `PT2M`" in body
    # 表の役割が一覧と各節の両方に出る
    assert body.count("恒久的なクラブ") >= 2
