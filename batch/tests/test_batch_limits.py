"""`api/src/config/batch-limits.ts` の列数が DDL と一致することを検査する。

**列数の唯一の出典は `db/migrations/*.sql` である**（詳細設計 3.4）。定数だけが古いまま
残ると、上限を静かに超えて 1 Worker 呼び出しあたり50クエリの制限に当たる。

v1.3 では表の列数が DDL と3テーブルでずれており、`games` / `team_game_stats` の上限が
200（正しくは160）になっていた。この検査はその再発を防ぐ。
"""
from __future__ import annotations

import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
LIMITS_TS = REPO_ROOT / "api" / "src" / "config" / "batch-limits.ts"
MIGRATIONS = REPO_ROOT / "db" / "migrations"

MAX_BIND_PARAMS = 100
STATEMENTS_BUDGET = 40
MAX_QUERIES_PER_REQUEST = 50


def ddl_column_counts() -> dict[str, int]:
    """`CREATE TABLE` の**全列数**を数える。

    `DEFAULT` を持つ列を除かない。実装が明示指定に変わった瞬間に上限を超えるため、
    最も厳しい側で固定する（詳細設計 3.4）。
    """
    sql = "\n".join(p.read_text(encoding="utf-8") for p in sorted(MIGRATIONS.glob("*.sql")))
    out: dict[str, int] = {}
    for table, raw in re.findall(r"CREATE TABLE (\w+) \((.*?)\n\);", sql, re.DOTALL):
        # **コメントを先に落とす。** 落とさないとコメント内のカンマで分割され、
        # 断片が列として数えられる（`-- 世代通番 1,2,3...` で predictions が
        # 19列から20列になった実例がある）。
        body = "\n".join(re.sub(r"--.*$", "", line) for line in raw.splitlines())
        depth, current, items = 0, "", []
        for ch in body:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if ch == "," and depth == 0:
                items.append(current)
                current = ""
            else:
                current += ch
        items.append(current)

        cols = 0
        for item in items:
            first = ""
            for line in item.splitlines():
                s = line.strip()
                if s and not s.startswith("--"):
                    first = s
                    break
            if not first:
                continue
            head = first.split()[0].upper()
            # 列ではない要素（表制約）を除く
            if head in {"PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "CONSTRAINT"}:
                continue
            cols += 1
        out[table] = cols

    # **CREATE の後にスキーマを動かす文も、出現順に畳む。** 追記のみの規約
    # （CLAUDE.md）では、列の追加は ALTER、制約の削除は「新テーブル → DROP →
    # RENAME」になる（0010）。落とすと定数側が正しくても「DDL と食い違う」と出るか、
    # 作業用テーブルが最終スキーマに残ったまま比較される。
    stripped = "\n".join(re.sub(r"--.*$", "", line) for line in sql.splitlines())
    mutation = re.compile(
        r"(?i)ALTER TABLE\s+(?P<added>\w+)\s+ADD COLUMN\b"
        r"|DROP TABLE\s+(?:IF EXISTS\s+)?(?P<dropped>\w+)"
        r"|ALTER TABLE\s+(?P<renamed>\w+)\s+RENAME TO\s+(?P<target>\w+)"
    )
    for match in mutation.finditer(stripped):
        if match.group("added"):
            table = match.group("added")
            assert table in out, f"ALTER の対象テーブルが DDL にない: {table}"
            out[table] += 1
        elif match.group("dropped"):
            out.pop(match.group("dropped"), None)
        else:
            source, target = match.group("renamed"), match.group("target")
            assert source in out, f"RENAME の対象テーブルが DDL にない: {source}"
            out[target] = out.pop(source)
    return out


def ts_column_counts() -> dict[str, int]:
    body = LIMITS_TS.read_text(encoding="utf-8")
    block = re.search(r"COLUMN_COUNTS = \{(.*?)\n\} as const;", body, re.DOTALL)
    assert block, "COLUMN_COUNTS が見つからない"
    return {m[0]: int(m[1]) for m in re.findall(r"(\w+):\s*(\d+),", block.group(1))}


def test_batch_limits_file_exists():
    assert LIMITS_TS.exists(), f"{LIMITS_TS} がない（工程4で作る）"


def test_batch_limits_match_schema():
    """定数の列数が DDL の全列数と一致すること。"""
    ddl = ddl_column_counts()
    ts = ts_column_counts()
    assert ts, "COLUMN_COUNTS が空"
    missing = sorted(set(ts) - set(ddl))
    assert not missing, f"DDL に無いテーブルが定数にある: {missing}"
    mismatched = {t: (ts[t], ddl[t]) for t in ts if ts[t] != ddl[t]}
    assert not mismatched, f"列数が DDL と食い違う (定数, DDL): {mismatched}"


def test_every_written_table_has_a_limit():
    """DDL にあるテーブルがすべて定数側に載っていること。

    載っていないテーブルへ書き込むコードが増えたとき、上限の計算が抜ける。
    """
    ddl = ddl_column_counts()
    ts = ts_column_counts()
    assert not sorted(set(ddl) - set(ts)), f"定数に載っていないテーブル: {sorted(set(ddl) - set(ts))}"


@pytest.mark.parametrize("table", sorted(ts_column_counts()))
def test_batch_size_within_query_limit(table):
    """各テーブルの上限が式どおりで、1リクエストの文数が50以下に収まること（A-10）。"""
    cols = ts_column_counts()[table]
    rows_per_stmt = MAX_BIND_PARAMS // cols
    assert rows_per_stmt >= 1, f"{table}: 1行も詰められない（列数 {cols}）"
    limit = rows_per_stmt * STATEMENTS_BUDGET
    statements = -(-limit // rows_per_stmt)          # 切り上げ
    assert statements <= MAX_QUERIES_PER_REQUEST, f"{table}: {statements} 文で上限超過"
    assert rows_per_stmt * cols <= MAX_BIND_PARAMS, f"{table}: バインドパラメータ超過"


def test_design_doc_table_matches_constants():
    """詳細設計 3.4 の表と定数が一致すること。

    文書が実装の後ろを走り始めないようにするための検査（CLAUDE.md）。
    """
    doc = (REPO_ROOT / "docs" / "design-detail.md").read_text(encoding="utf-8")
    ts = ts_column_counts()
    checked = 0
    for line in doc.splitlines():
        m = re.match(r"^\|\s*`(\w+)`\s*\|\s*(\d+)\s*\|", line)
        if not m:
            continue
        table, cols = m.group(1), int(m.group(2))
        if table in ts:
            assert ts[table] == cols, f"{table}: 文書 {cols} 列 / 定数 {ts[table]} 列"
            checked += 1
    assert checked >= 10, f"文書から照合できた行が少なすぎる: {checked}"
