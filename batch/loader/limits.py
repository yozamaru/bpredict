"""1リクエストあたりの行数上限。**DDL を唯一の出典にする。**

    max_rows_per_request = floor(100 / 列数) × 40

バインドパラメータ上限100と、D1 Free の1呼び出し50クエリから決まる（詳細設計 3.4）。
係数40は、50クエリのうち10を非活性化・ログ・整合性確認の余裕として残すため。

**列数を Python 側に書き写さない。** `api/src/config/batch-limits.ts` にも同じ値があるが、
どちらも DDL から数えたものであり、写すと片方だけ古くなる。ここでは
`db/migrations/*.sql` を読んで数える（`test_batch_limits.py` が両者の一致を検査する）。
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

MAX_BIND_PARAMS = 100
MAX_QUERIES_PER_REQUEST = 50
STATEMENTS_BUDGET = 40

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "db" / "migrations"

_TABLE = re.compile(r"CREATE TABLE (\w+)\s*\((.*?)\n\);", re.DOTALL)
#: 追記のみの規約（CLAUDE.md）のもとでは、列の追加は ALTER になる。
#: これを数えないと上限が古い列数で算出され、バインドパラメータ上限を静かに超える。
_ADD_COLUMN = re.compile(r"(?i)ALTER TABLE\s+(\w+)\s+ADD COLUMN\b")


class LimitError(KeyError):
    """DDL に存在しないテーブル名。"""


@lru_cache(maxsize=1)
def _column_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        for name, body in _TABLE.findall(path.read_text(encoding="utf-8")):
            # **コメントを先に落とす。** 落とさないとコメント内のカンマで分割され、
            # 断片が列として数えられる（`-- 世代通番 1,2,3...` で実際に起きた）
            cleaned = "\n".join(re.sub(r"--.*$", "", line) for line in body.splitlines())
            depth = 0
            columns = 0
            current = ""
            for character in cleaned + ",":
                if character == "(":
                    depth += 1
                elif character == ")":
                    depth -= 1
                if character == "," and depth == 0:
                    token = current.strip()
                    if token and not re.match(
                        r"(?i)^(PRIMARY KEY|FOREIGN KEY|UNIQUE|CHECK|CONSTRAINT)\b", token
                    ):
                        columns += 1
                    current = ""
                else:
                    current += character
            counts[name] = columns
    # ALTER は CREATE より後のファイルに来るため、全ファイルを読んだ後に足す
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        body = path.read_text(encoding="utf-8")
        stripped = "\n".join(re.sub(r"--.*$", "", line) for line in body.splitlines())
        for name in _ADD_COLUMN.findall(stripped):
            if name not in counts:
                raise LimitError(f"ALTER の対象テーブルが DDL にない: {name}")
            counts[name] += 1
    return counts


def column_count(table: str) -> int:
    counts = _column_counts()
    if table not in counts:
        raise LimitError(table)
    return counts[table]


def rows_per_statement(table: str) -> int:
    """1文に詰められる行数。バインドパラメータ上限で決まる。"""
    return max(1, MAX_BIND_PARAMS // column_count(table))


def max_rows_per_request(table: str) -> int:
    return rows_per_statement(table) * STATEMENTS_BUDGET


def chunks(table: str, rows: list[dict[str, object]]) -> list[list[dict[str, object]]]:
    """1リクエストに収まる大きさへ分割する。"""
    size = max_rows_per_request(table)
    return [rows[index : index + size] for index in range(0, len(rows), size)]
