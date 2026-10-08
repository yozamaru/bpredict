"""DB の現状を Markdown に書き出す（`docs/db-schema.md` を生成する）。

    python3 scripts/describe_schema.py --database <sqlite> --output docs/db-schema.md
    python3 scripts/describe_schema.py --database <sqlite> --html /tmp/db-schema.html

**`--html` の出力はリポジトリに入れない。** `.html` の追跡は
`scripts/check_fixtures.py` が全面禁止している（モックの HTML と同じ扱い）。
スマホで読むときは Artifact として公開する。**正本は `docs/db-schema.md` である。**

**手で書かない。** 構造は `db/migrations/*.sql` を in-memory SQLite に適用して読み、
列の説明は DDL のコメントから抜く（`sqlite_master.sql` が原文を保持している）。
行数と列ごとの充填率は、D1 からエクスポートした SQLite を測る。

**定義と理由は `docs/design-detail.md` 1章にある。** この文書が持つのは
「いま実際に何が入っているか」だけで、設計の説明を二重に書かない
（CLAUDE.md「二重に書くと必ず片方が古くなる」）。
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from schema_notes import (
    describe_column,
    describe_table,
    source_field,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"

#: マイグレーションのファイル名 → 分類（基本設計 3.1）。番号から機械的に決まる
CATEGORY = {
    "0001": "マスタ",
    "0002": "ファクト",
    "0003": "派生",
    "0004": "評価（モデル）",
    "0005": "予測",
    "0006": "評価",
    "0007": "運用",
    "0012": "集計",
}

#: **0行であることに理由がある表**だけを書く。理由は設計への参照に限る。
#:
#: **「同上」と書かない。** 表はアルファベット順に並ぶため、順序が変わると
#: 「同上」が別の行を指す（実際に `accuracy_summary` が先頭に来て意味を失った）。
EMPTY_REASON = {
    "game_entries": "取得するのは `gameday_update` で、まだ実装されていない（詳細設計 4.1）",
    "player_seasons": "登録区分とポジションの正規化が未決のため backfill が作らない（詳細設計 3.4）",
    "predictions": "推論の結線は工程9b（詳細設計 9章）",
    "player_predictions": "親の `predictions` が空（工程9b）",
    "prediction_reasons": "親の `predictions` が空（工程9b）",
    "prediction_factors": "親の `predictions` が空（工程9b）",
    "prediction_team_targets": "親の `predictions` が空（工程9b）",
    "prediction_model_bundle": "親の `predictions` が空（工程9b）",
    "prediction_results": "照合は予測が入ってから（工程9b 以降）",
    "accuracy_summary": "`prediction_results` を畳んだ表。照合が始まってから（工程9b 以降）",
    "player_stat_summary": "集計ジョブは工程16（詳細設計 4.14）。**スナップショット側では 4,587 行**",
    "team_stat_summary": "同上。**スナップショット側では 268 行**",
    "model_versions": "学習済みモデルの登録は工程8",
    "team_ratings": "**スナップショット側には 12,532 行ある。** D1 への書き戻しが未実施（詳細設計 4.1 の `rebuild-derived`）",
    "venue_revisions": "**スナップショット側には 173 区間ある。** D1 への書き戻しが未実施（同上の `rebuild-derived`）",
}


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    notnull: bool
    default: str | None
    pk: int
    #: DDL の `--` コメント。**あればこれを優先する**
    comment: str
    #: CHECK から読んだ許容値（`status IN ('SCHEDULED',...)`）
    allowed: tuple[str, ...] = ()
    #: CHECK から読んだ値域（`BETWEEN 0 AND 1`）
    span: tuple[str, str] | None = None
    #: 表の役割から決まる説明（`scripts/schema_notes.py`）
    note: str = ""
    #: 取得元のフィールド名（詳細設計 4.4 の対応表）
    source: str = ""
    #: FK から自動で導いた参照先
    refers: str = ""

    @property
    def description(self) -> str:
        """DDL コメント → 注記 → FK 由来 の順で決める。**無ければ空。**"""
        if self.comment:
            return self.comment
        if self.note:
            return self.note
        if self.refers:
            return f"`{self.refers}` への参照"
        return ""


def migration_of(table: str) -> tuple[str, str]:
    """その表を作ったマイグレーションの番号と分類。"""
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if re.search(rf"CREATE TABLE {re.escape(table)}\s*\(", path.read_text(encoding="utf-8")):
            number = path.name.split("_", 1)[0]
            return number, CATEGORY.get(number, "—")
    return "—", "—"


def structure() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        con.executescript(path.read_text(encoding="utf-8"))
    return con


def comments_of(ddl: str, names: list[str]) -> dict[str, str]:
    """DDL の `--` コメントを列に割り当てる。

    列定義の行にあるコメントと、**その直後のコメントだけの行**を拾う
    （DDL は折り返して書かれており、説明が次行に続くことがある）。
    """
    lines = ddl.splitlines()
    starts: dict[int, str] = {}
    for i, line in enumerate(lines):
        token = line.strip().split(" ", 1)[0].strip(",")
        if token in names and token not in starts.values():
            starts[i] = token

    out: dict[str, list[str]] = {name: [] for name in names}
    for i, name in sorted(starts.items()):
        for line in lines[i:]:
            body, sep, comment = line.partition("--")
            if line is not lines[i] and body.strip():
                break  # 次の列の定義に入った
            if sep:
                out[name].append(comment.strip())
            if line is not lines[i] and not sep:
                break
    return {name: " ".join(parts) for name, parts in out.items()}


def allowed_of(ddl: str, column: str) -> tuple[str, ...]:
    """`CHECK (col IN ('A','B'))` の許容値。**何を取り込むかが列から読める。**"""
    m = re.search(rf"CHECK\s*\(\s*{re.escape(column)}\s+IN\s*\(([^)]*)\)", ddl)
    if not m:
        return ()
    return tuple(v.strip().strip("'") for v in m.group(1).split(",") if v.strip())


def span_of(ddl: str, column: str) -> tuple[str, str] | None:
    """`CHECK (col BETWEEN 0 AND 1)` の値域。"""
    m = re.search(
        rf"{re.escape(column)}\s+BETWEEN\s+([^\s]+)\s+AND\s+([^\s)]+)", ddl,
    )
    return (m.group(1), m.group(2)) if m else None


def columns_of(con: sqlite3.Connection, table: str) -> list[Column]:
    rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    ddl = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,),
    ).fetchone()[0]
    comments = comments_of(ddl, [str(r[1]) for r in rows])
    fks = {
        str(row[3]): f"{row[2]}.{row[4]}"
        for row in con.execute(f"PRAGMA foreign_key_list({table})")
    }
    return [
        Column(
            name=str(r[1]), type=str(r[2]) or "—", notnull=bool(r[3]),
            default=None if r[4] is None else str(r[4]), pk=int(r[5]),
            comment=comments.get(str(r[1]), ""),
            allowed=allowed_of(ddl, str(r[1])),
            span=span_of(ddl, str(r[1])),
            note=describe_column(table, str(r[1])),
            source=source_field(table, str(r[1])),
            refers=fks.get(str(r[1]), ""),
        )
        for r in rows
    ]


def measure(data: sqlite3.Connection | None, table: str, columns: list[Column]) -> tuple[int | None, dict[str, int]]:
    if data is None:
        return None, {}
    try:
        total = int(data.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except sqlite3.Error:
        return None, {}
    if total == 0:
        return 0, {c.name: 0 for c in columns}
    picks = ", ".join(f'COUNT("{c.name}")' for c in columns)
    filled = data.execute(f"SELECT {picks} FROM {table}").fetchone()
    return total, {c.name: int(v) for c, v in zip(columns, filled, strict=True)}


def fill_label(total: int | None, filled: int) -> str:
    if total is None:
        return "—"
    if total == 0:
        return "—"
    if filled == total:
        return "100%"
    if filled == 0:
        return "**0%**"
    return f"{filled / total:.0%}"


def render(con: sqlite3.Connection, data: sqlite3.Connection | None, *, source: str) -> str:
    tables = [
        str(r[0]) for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name",
        )
    ]
    measured: dict[str, tuple[int | None, dict[str, int]]] = {}
    cols: dict[str, list[Column]] = {}
    for table in tables:
        cols[table] = columns_of(con, table)
        measured[table] = measure(data, table, cols[table])

    now = datetime.now(UTC).strftime("%Y-%m-%d")
    out: list[str] = [
        "# B.PREDICT データベース現状",
        "",
        "| 項目 | 内容 |",
        "|---|---|",
        "| 生成 | **`python3 scripts/describe_schema.py` が生成する。手で編集しない** |",
        f"| 生成日 | {now} |",
        "| 構造の出典 | `db/migrations/*.sql`（列の説明は DDL のコメント） |",
        f"| 行数の出典 | {source} |",
        "| 定義と設計の理由 | **`docs/design-detail.md` 1章**。この文書では繰り返さない |",
        "",
        "この文書は「**いま実際に何が入っているか**」だけを扱う。列の意味・制約の理由・",
        "トリガの設計意図は詳細設計1章にあり、ここに二重に書かない。",
        "",
        "## 表の一覧",
        "",
        "| 表 | 分類 | 列数 | 行数 | 役割 |",
        "|---|---|---:|---:|---|",
    ]
    for table in tables:
        _, category = migration_of(table)
        total, _ = measured[table]
        rows = "—" if total is None else f"{total:,}"
        out.append(
            f"| [`{table}`](#{table}) | {category} | {len(cols[table])} | {rows} | "
            f"{describe_table(table)} |",
        )

    filled_tables = [t for t in tables if (measured[t][0] or 0) > 0]
    empty_tables = [t for t in tables if measured[t][0] == 0]
    if data is not None:
        out += [
            "",
            f"**{len(filled_tables)} 表にデータがあり、{len(empty_tables)} 表が空である。**",
            "",
            "### 空の表とその理由",
            "",
            "| 表 | 理由 |",
            "|---|---|",
        ]
        for table in empty_tables:
            out.append(f"| `{table}` | {EMPTY_REASON.get(table, '**理由が未記載**')} |")

    out += ["", "## 関連（ER図）", ""]
    out += [
        "**手で描かない。** 図は `db/migrations/*.sql` の FK 定義そのものである。",
        "多重度も DDL から導く — 親側は子の FK 列が NOT NULL なら `||`、NULL 可なら `|o`。",
        "子側は FK 列が子の主キーそのものなら `||`（1:1）、そうでなければ `o{`。",
        "",
        "**24表を1枚にしない。** 分類ごとに3枚へ分け、各図はその群の子テーブルと",
        "その親（別の群にあっても）を含む。したがって図をまたいで同じ表が現れる。",
        "",
    ]
    for title, note, body, loops in er_diagrams(con):
        out += [f"### {title}", "", note, "", "```mermaid", body, "```", ""]
        if loops:
            out += ["**自己参照**（図には入れていない）", "", "| 表 | 列 | 参照先 |", "|---|---|---|"]
            out += [f"| `{c}` | `{col}` | `{par}.id` |" for c, col, par in loops]
            out.append("")

    out += ["", "## 表ごとの列", ""]
    for table in tables:
        total, filled = measured[table]
        number, category = migration_of(table)
        out += [
            f"### {table}",
            "",
            f"{category} ／ `db/migrations/{number}_*.sql` ／ "
            + ("行数 —" if total is None else f"**{total:,} 行**"),
            "",
        ]
        if describe_table(table):
            out += [describe_table(table), ""]
        if total == 0 and table in EMPTY_REASON:
            out += [f"> {EMPTY_REASON[table]}", ""]
        out += [
            "| 列 | 型 | NULL | 既定値 | 値あり | 説明 |",
            "|---|---|---|---|---:|---|",
        ]
        for c in cols[table]:
            null = "不可" if (c.notnull or c.pk) else "可"
            default = f"`{c.default}`" if c.default is not None else "—"
            key = " 🔑" if c.pk else ""
            desc = c.description
            if c.allowed:
                desc += " 許容値: " + " / ".join(f"`{v}`" for v in c.allowed)
            if c.span:
                desc += f" 値域: `{c.span[0]}`〜`{c.span[1]}`"
            if c.source:
                desc += f" 取得元: `{c.source}`"
            out.append(
                f"| `{c.name}`{key} | {c.type} | {null} | {default} | "
                f"{fill_label(total, filled.get(c.name, 0))} | {desc.strip()} |",
            )
        out.append("")

    out += [
        "## トリガ",
        "",
        "**予測の凍結を守る関門である**（詳細設計 1.8）。`is_final = 1` の行とその子は",
        "更新・削除できない。",
        "",
        "| トリガ | 対象の表 |",
        "|---|---|",
    ]
    for name, tbl in con.execute(
        "SELECT name, tbl_name FROM sqlite_master WHERE type='trigger' ORDER BY tbl_name, name",
    ):
        out.append(f"| `{name}` | `{tbl}` |")
    out.append("")
    return "\n".join(out)



# --- ER図（Mermaid）。FK は `PRAGMA foreign_key_list` から取る ---
#
# **関連を手で描かない。** 図は DDL の FK 定義そのものである。
# **24表を1枚にしない** — 分類（基本設計 3.1）ごとに3枚に分け、各図はその群の
# 子テーブルと、その親（別の群にあっても）を含む自己完結の形にする。

#: 図の単位。マイグレーションの番号で分ける（基本設計 3.1 の分類に対応する）
ER_GROUPS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "マスタ", ("0001",),
        ("恒久エンティティ（`clubs` / `players` / `venues` / `seasons`）と、"
        "年度断面・履歴・名寄せ。**時間で変わるものを単一行の属性として持たない**"
        ),
    ),
    (
        "ファクトと派生", ("0002", "0003"),
        ("試合ごとに増える表と、バッチが全期間を再計算する派生表。"
        "`team_games` は `games` への JOIN を消すためにある"
        ),
    ),
    (
        "予測と評価", ("0004", "0005", "0006", "0007"),
        "予測は**追記のみ**で、`is_final = 1` の行とその子は凍結される（詳細設計 1.8）",
    ),
)


@dataclass(frozen=True)
class Relation:
    parent: str
    child: str
    column: str
    optional: bool
    one_to_one: bool

    def mermaid(self) -> str:
        """`親 <親側>--<子側> 子` の形。

        **左の記号は「子1件に対して親が何件か」**、右は「親1件に対して子が何件か」。
        Mermaid の `CUSTOMER ||--o{ ORDER` が「注文1件には顧客がちょうど1人、
        顧客1人には注文が0件以上」と読む規約に従う。

        **1:1 の子側は `||` ではなく `|o` である。** FK が子の主キーなら子は
        最大1件だが、**親に子が無いこともある**（`predictions` に対応する
        `prediction_results` は照合前には存在しない）。
        """
        left = "|o" if self.optional else "||"
        right = "|o" if self.one_to_one else "o{"
        return f'    {self.parent} {left}--{right} {self.child} : "{self.column}"'


def relations(con: sqlite3.Connection, tables: list[str]) -> list[Relation]:
    """FK の一覧。**多重度は DDL から導く**（推測しない）。

    - 親側: 子の FK 列が NOT NULL なら `||`（必ず1つ）、NULL 可なら `|o`
    - 子側: FK 列が子の主キーそのものなら `|o`（最大1件）、そうでなければ `o{`

    **主キーの列は NOT NULL として扱う。** SQLite は非 INTEGER の主キー列に
    NULL を許すが（`notnull` が 0 で返る）、主キーに NULL を入れる運用はしない。
    そのまま読むと「親のない子がありうる」という誤った図になる。
    """
    out: list[Relation] = []
    for child in tables:
        info = {str(r[1]): r for r in con.execute(f"PRAGMA table_info({child})")}
        pk = {name for name, r in info.items() if int(r[5]) > 0}
        for row in con.execute(f"PRAGMA foreign_key_list({child})"):
            parent, column = str(row[2]), str(row[3])
            if column not in info:
                continue
            out.append(Relation(
                parent=parent, child=child, column=column,
                optional=not (bool(info[column][3]) or int(info[column][5]) > 0),
                one_to_one=pk == {column},
            ))
    return out


def er_diagrams(con: sqlite3.Connection) -> list[tuple[str, str, str, list[tuple[str, str, str]]]]:
    """(見出し, 説明, Mermaid の本文, 自己参照の一覧) を群ごとに返す。"""
    tables = [
        str(r[0]) for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name",
        )
    ]
    rels = relations(con, tables)
    out = []
    for title, numbers, note in ER_GROUPS:
        members = [t for t in tables if migration_of(t)[0] in numbers]
        # その群の子テーブルに関わる関連だけを引く（親は別の群でも含める）
        picked = [r for r in rels if r.child in members]
        # **自己参照は図に入れない。** Mermaid の描画が不安定で、
        # 1件の自己ループで図全体が出なくなる事故を避ける（別表で示す）
        loops = [(r.child, r.column, r.parent) for r in picked if r.parent == r.child]
        picked = [r for r in picked if r.parent != r.child]
        entities = sorted({r.parent for r in picked} | {r.child for r in picked} | set(members))
        lines = ["erDiagram"]
        # FK を持たない表は**名前だけ**で宣言する。空の属性ブロック（`{ }`）は
        # Mermaid の構文として危うく、1箇所で図全体が出なくなる
        lines += [f"    {name}" for name in entities if not any(
            r.parent == name or r.child == name for r in picked
        )]
        lines += [r.mermaid() for r in sorted(picked, key=lambda r: (r.child, r.column))]
        out.append((title, note, "\n".join(lines), loops))
    return out


# --- HTML（スマホで読むための1枚。Markdown と同じ測定値から作る） ---
#
# **プロジェクトのデザインシステムに従う**（基本設計 6章の方向性A）。
# 藍はデータの色、`--warn` は「注意状態」のトークンで 0% に当てる。
# 書体は3系統ですべてシステムフォント — **Web フォントを読み込まない**。

def esc(text: str) -> str:
    out = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    parts = out.split("`")
    return "".join(
        p if i % 2 == 0 else f"<code>{p}</code>" for i, p in enumerate(parts)
    )


HTML_STYLE = """<style>
:root{
  --paper:#F7F5EF; --panel:#FFFFFF; --tint:#F2EFE6;
  --ink:#15181D; --ink-2:#4B515B; --ink-3:#676D78;
  --data:#134A83; --warn:#7A4A12; --warn-bg:#FBF1E2;
  --rule:#D9D3C5; --rule-soft:#E7E2D6; --band:#E3DBC4;
  --f-serif:"Hiragino Mincho ProN","Yu Mincho",YuMincho,"Noto Serif JP",serif;
  --f-sans:-apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans JP","Yu Gothic UI",system-ui,sans-serif;
  --f-mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace;
  color-scheme:light;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --paper:#0B0D11; --panel:#12151A; --tint:#161A20;
    --ink:#E9ECF2; --ink-2:#A9B2BF; --ink-3:#8D97A5;
    --data:#79B2F7; --warn:#E0A458; --warn-bg:rgba(224,164,88,.12);
    --rule:#242A33; --rule-soft:#1C212A; --band:#252D38;
    color-scheme:dark;
  }
}
:root[data-theme="dark"]{
  --paper:#0B0D11; --panel:#12151A; --tint:#161A20;
  --ink:#E9ECF2; --ink-2:#A9B2BF; --ink-3:#8D97A5;
  --data:#79B2F7; --warn:#E0A458; --warn-bg:rgba(224,164,88,.12);
  --rule:#242A33; --rule-soft:#1C212A; --band:#252D38;
  color-scheme:dark;
}
*{box-sizing:border-box}
body{background:var(--paper);color:var(--ink);font-family:var(--f-sans);
  font-size:13px;line-height:1.65;margin:0}
.num{font-family:var(--f-mono);font-variant-numeric:tabular-nums}
.wrap{max-width:560px;margin:0 auto;padding-inline:16px;padding-block:0 56px}

header.top{position:sticky;top:env(safe-area-inset-top,0px);z-index:5;
  background:var(--paper);border-bottom:1px solid var(--rule);
  padding-block:10px;margin-bottom:18px}
h1{font-family:var(--f-serif);font-size:19px;font-weight:600;margin:0;
  letter-spacing:.01em;text-wrap:balance}
.sub{color:var(--ink-3);font-size:11px;margin-top:2px}
.tools{display:flex;gap:8px;margin-top:10px}
input[type=search]{flex:1 1 auto;min-width:0;font-family:var(--f-sans);font-size:13px;
  padding:8px 10px;border:1px solid var(--rule);border-radius:2px;
  background:var(--panel);color:var(--ink)}
button{font-family:var(--f-sans);font-size:11px;font-weight:700;letter-spacing:.04em;
  padding:8px 10px;min-height:36px;border:1px solid var(--rule);border-radius:2px;
  background:var(--panel);color:var(--ink-2);cursor:pointer}
button[aria-pressed=true]{background:var(--tint);color:var(--ink);border-color:var(--ink-3)}
:focus-visible{outline:2px solid var(--ink);outline-offset:2px}

dl.meta{display:grid;grid-template-columns:auto 1fr;gap:2px 12px;margin:0 0 20px;
  font-size:12px}
dl.meta dt{color:var(--ink-3)}
dl.meta dd{margin:0}
h2{font-family:var(--f-serif);font-size:16px;font-weight:600;
  margin:26px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--rule)}
p.note{color:var(--ink-2);margin:0 0 14px}

table.index{width:100%;border-collapse:collapse;font-size:12px}
table.index th{text-align:left;font-size:10px;font-weight:700;letter-spacing:.06em;
  color:var(--ink-3);border-bottom:1px solid var(--rule);padding:4px 6px 4px 0}
table.index td{border-bottom:1px solid var(--rule-soft);padding:6px 6px 6px 0;
  vertical-align:baseline}
table.index th.r,table.index td.r{text-align:right;padding-right:0}
table.index a{color:var(--ink);text-decoration:none;border-bottom:1px solid var(--rule);
  font-family:var(--f-mono)}
tr.zero td{color:var(--ink-3)}

section.tbl{border:1px solid var(--rule);border-radius:2px;background:var(--panel);
  margin-bottom:10px}
section.tbl>summary{list-style:none}
details>summary{cursor:pointer;padding:10px 12px;min-height:44px;
  display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}
details>summary::-webkit-details-marker{display:none}
details>summary::after{content:"＋";margin-left:auto;color:var(--ink-3);
  font-family:var(--f-mono)}
details[open]>summary::after{content:"−"}
details[open]>summary{border-bottom:1px solid var(--rule-soft);background:var(--tint)}
.tname{font-family:var(--f-mono);font-size:14px;font-weight:600}
.cat{font-size:10px;font-weight:700;letter-spacing:.06em;color:var(--ink-3)}
.rows{margin-left:auto;font-family:var(--f-mono);font-variant-numeric:tabular-nums;
  font-size:12px;color:var(--ink-2)}
.rows.zero{color:var(--warn);background:var(--warn-bg);padding:1px 6px;border-radius:2px}
.why{margin:10px 12px;padding:8px 10px;background:var(--warn-bg);color:var(--warn);
  font-size:12px;border-radius:2px}
.cols{padding:4px 0 8px}
.er{padding:4px 12px 12px}
.role{color:var(--ink-2);font-size:12px;margin:10px 12px 0}
.rolemini{color:var(--ink-3);font-size:11px;line-height:1.45;margin-top:2px;
  font-family:var(--f-sans)}
.chip.src{color:var(--data);border-color:var(--data);font-family:var(--f-mono);
  font-weight:400;letter-spacing:0}
.chip.val{background:var(--tint);color:var(--ink-2);font-family:var(--f-mono);
  font-weight:400;letter-spacing:0}
pre.mermaid{margin:8px 0 0;overflow-x:auto;background:var(--tint);border-radius:2px;
  padding:8px;font-family:var(--f-mono);font-size:11px;line-height:1.5}
.col{padding:8px 12px;border-top:1px solid var(--rule-soft)}
.col:first-child{border-top:none}
.cname{font-family:var(--f-mono);font-size:13px;font-weight:600;overflow-wrap:anywhere}
.cname .pk{color:var(--data);font-weight:700}
.chips{display:flex;flex-wrap:wrap;gap:4px;margin:4px 0 0}
.chip{font-size:10px;font-weight:700;letter-spacing:.04em;color:var(--ink-3);
  border:1px solid var(--rule);border-radius:2px;padding:0 5px;white-space:nowrap}
.chip.nn{color:var(--ink-2);border-color:var(--ink-3)}
.chip.def{font-family:var(--f-mono);font-weight:400;letter-spacing:0}
.fill{display:flex;align-items:center;gap:8px;margin-top:6px}
.bar{position:relative;flex:1 1 auto;height:4px;background:var(--band);border-radius:2px}
.bar i{position:absolute;inset:0 auto 0 0;background:var(--data);border-radius:2px}
.pct{font-family:var(--f-mono);font-variant-numeric:tabular-nums;font-size:11px;
  color:var(--ink-2);min-width:3.2em;text-align:right}
.pct.zero{color:var(--warn);font-weight:700}
.pct.part{color:var(--warn)}
.cmt{color:var(--ink-2);font-size:12px;margin-top:4px}
.cmt code,dl.meta code,p.note code,.why code{font-family:var(--f-mono);font-size:.92em;
  background:var(--tint);padding:0 3px;border-radius:2px}
.empty{color:var(--ink-3);font-size:12px;padding:10px 12px}
footer{margin-top:28px;padding-top:12px;border-top:1px solid var(--rule);
  color:var(--ink-3);font-size:11px}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
</style>
"""


def html_fill(total: int | None, filled: int) -> str:
    if total is None or total == 0:
        return '<div class="fill"><div class="bar"></div><span class="pct">—</span></div>'
    ratio = filled / total
    cls = "zero" if filled == 0 else ("part" if ratio < 0.999 else "")
    label = "0%" if filled == 0 else f"{ratio:.0%}"
    return (
        f'<div class="fill"><div class="bar"><i style="width:{ratio:.1%}"></i></div>'
        f'<span class="pct {cls}">{label}</span></div>'
    )


def render_html(con: sqlite3.Connection, data: sqlite3.Connection | None, *, source: str) -> str:
    tables = [
        str(r[0]) for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name",
        )
    ]
    cols = {t: columns_of(con, t) for t in tables}
    measured = {t: measure(data, t, cols[t]) for t in tables}
    now = datetime.now(UTC).strftime("%Y-%m-%d")
    n_filled = sum(1 for t in tables if (measured[t][0] or 0) > 0)
    n_empty = sum(1 for t in tables if measured[t][0] == 0)

    out: list[str] = ["<title>B.PREDICT データベース現状</title>", HTML_STYLE, '<div class="wrap">']
    out += [
        '<header class="top">',
        "<h1>B.PREDICT データベース現状</h1>",
        f'<div class="sub">{esc(source)} ／ {now} 生成</div>',
        '<div class="tools">',
        '<input type="search" id="q" placeholder="表や列の名前で絞り込む" autocomplete="off">',
        '<button type="button" id="zero" aria-pressed="false">空の表を隠す</button>',
        '<button type="button" id="all" aria-pressed="false">全部開く</button>',
        "</div></header>",
    ]
    out += [
        '<dl class="meta">',
        "<dt>構造</dt><dd><code>db/migrations/*.sql</code>（列の説明は DDL のコメント）</dd>",
        f"<dt>行数</dt><dd>{esc(source)}</dd>",
        "<dt>生成</dt><dd><code>scripts/describe_schema.py</code>。手で編集しない</dd>",
        "<dt>定義と理由</dt><dd><code>docs/design-detail.md</code> 1章。ここでは繰り返さない</dd>",
        "</dl>",
        (f'<p class="note">24表のうち<strong>{n_filled}表にデータがあり、{n_empty}表が空</strong>である。'
        "この文書が示すのは「いま実際に何が入っているか」だけで、"
        "列の意味・制約の理由・トリガの設計意図は詳細設計1章にある。</p>"
        ),
    ]

    out += [
        "<h2>表の一覧</h2>",
        ('<table class="index"><thead><tr>'
        '<th>表</th><th>分類</th><th class="r">列</th><th class="r">行</th>'
        "</tr></thead><tbody>"
        ),
    ]
    for t in tables:
        total, _ = measured[t]
        _, category = migration_of(t)
        rows = "—" if total is None else f"{total:,}"
        zero = ' class="zero"' if total == 0 else ""
        out.append(
            f'<tr{zero}><td><a href="#t-{t}">{t}</a></td><td>{category}</td>'
            f'<td class="r num">{len(cols[t])}</td><td class="r num">{rows}</td></tr>',
        )
    out.append("</tbody></table>")

    out.append("<h2>関連（ER図）</h2>")
    out.append(
        '<p class="note"><strong>手で描かない。</strong>図は <code>db/migrations/*.sql</code> の'
        " FK 定義そのものである。多重度も DDL から導く。"
        "<strong>24表を1枚にせず</strong>、分類ごとに3枚へ分けてある"
        "（図をまたいで同じ表が現れる）。</p>",
    )
    for title, note, body, loops in er_diagrams(con):
        out.append(f'<details class="tbl" open><summary><span class="tname">{title}</span></summary>')
        out.append(f'<div class="er"><p class="note">{esc(note)}</p>')
        out.append(f'<pre class="mermaid">{esc(body)}</pre>')
        if loops:
            out.append('<p class="note"><strong>自己参照</strong>（図には入れていない）: ')
            out.append(
                " / ".join(f"<code>{c}.{col}</code> → <code>{par}.id</code>" for c, col, par in loops),
            )
            out.append("</p>")
        out.append("</div></details>")

    out.append("<h2>表ごとの列</h2>")
    for t in tables:
        total, filled = measured[t]
        number, category = migration_of(t)
        is_empty = total == 0
        rows = "—" if total is None else f"{total:,} 行"
        out += [
            f'<details class="tbl" id="t-{t}" data-table="{t}"{"" if is_empty else " open"}>',
            (f'<summary><span class="tname">{t}</span>'
            f'<span class="cat">{category} · {number}</span>'
            f'<span class="rows{" zero" if is_empty else ""}">{rows}</span></summary>'
            ),
        ]
        if is_empty and t in EMPTY_REASON:
            out.append(f'<div class="why">{esc(EMPTY_REASON[t])}</div>')
        if describe_table(t):
            out.append(f'<p class="role">{esc(describe_table(t))}</p>')
        out.append('<div class="cols">')
        for c in cols[t]:
            chips = [f'<span class="chip">{c.type}</span>']
            if c.notnull or c.pk:
                chips.append('<span class="chip nn">NOT NULL</span>')
            if c.default is not None:
                chips.append(f'<span class="chip def">= {esc(c.default)}</span>')
            if c.source:
                chips.append(f'<span class="chip src">取得元 {esc(c.source)}</span>')
            for value in c.allowed:
                chips.append(f'<span class="chip val">{esc(value)}</span>')
            if c.span:
                chips.append(
                    f'<span class="chip def">{esc(c.span[0])}〜{esc(c.span[1])}</span>',
                )
            key = ' <span class="pk">🔑</span>' if c.pk else ""
            cmt = f'<div class="cmt">{esc(c.description)}</div>' if c.description else ""
            out += [
                f'<div class="col" data-col="{c.name}">',
                f'<div class="cname">{c.name}{key}</div>',
                f'<div class="chips">{"".join(chips)}</div>',
                html_fill(total, filled.get(c.name, 0)),
                cmt,
                "</div>",
            ]
        out += ["</div>", "</details>"]

    out += [
        "<h2>トリガ</h2>",
        ('<p class="note"><strong>予測の凍結を守る関門である</strong>（詳細設計 1.8）。'
        "<code>is_final = 1</code> の行とその子は更新・削除できない。</p>"
        ),
        '<table class="index"><thead><tr><th>トリガ</th><th>対象の表</th></tr></thead><tbody>',
    ]
    for name, tbl in con.execute(
        "SELECT name, tbl_name FROM sqlite_master WHERE type='trigger' ORDER BY tbl_name, name",
    ):
        out.append(f'<tr><td class="num">{name}</td><td class="num">{tbl}</td></tr>')
    out += ["</tbody></table>"]

    out += [
        ("<footer>構造は <code>db/migrations/*.sql</code>、行数は D1 の実データ。"
        "<code>python3 scripts/describe_schema.py --html</code> で作り直す。</footer>"
        ),
        "</div>",
        """<script>
(function(){
  var q=document.getElementById('q'), zero=document.getElementById('zero'),
      all=document.getElementById('all'),
      secs=Array.prototype.slice.call(document.querySelectorAll('details.tbl')),
      rows=Array.prototype.slice.call(document.querySelectorAll('table.index tbody tr'));
  function apply(){
    var term=(q.value||'').trim().toLowerCase(),
        hideZero=zero.getAttribute('aria-pressed')==='true';
    secs.forEach(function(s){
      var name=s.dataset.table.toLowerCase(),
          cols=Array.prototype.slice.call(s.querySelectorAll('.col')),
          isZero=!!s.querySelector('.rows.zero'), hit=!term||name.indexOf(term)>=0, any=hit;
      cols.forEach(function(c){
        var m=!term||hit||c.dataset.col.toLowerCase().indexOf(term)>=0;
        c.hidden=!m; if(m){any=true;}
      });
      s.hidden=!any||(hideZero&&isZero);
      if(term&&!s.hidden){s.open=true;}
    });
    rows.forEach(function(r){
      var a=r.querySelector('a'), name=a?a.textContent.toLowerCase():'',
          isZero=r.classList.contains('zero');
      r.hidden=(!!term&&name.indexOf(term)<0)||(hideZero&&isZero);
    });
  }
  q.addEventListener('input',apply);
  zero.addEventListener('click',function(){
    zero.setAttribute('aria-pressed',zero.getAttribute('aria-pressed')==='true'?'false':'true');
    apply();
  });
  all.addEventListener('click',function(){
    var open=all.getAttribute('aria-pressed')!=='true';
    all.setAttribute('aria-pressed',open?'true':'false');
    all.textContent=open?'全部閉じる':'全部開く';
    secs.forEach(function(s){s.open=open;});
  });
})();
</script>""",
    ]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DB の現状を Markdown に書き出す")
    parser.add_argument("--database", type=Path, help="D1 からエクスポートした SQLite")
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "docs" / "db-schema.md")
    parser.add_argument("--source", default="", help="行数の出典として書く説明")
    parser.add_argument("--html", type=Path, help="スマホで読む1枚の HTML を書き出す先")
    args = parser.parse_args(argv)

    data = None
    source = args.source or "未測定（`--database` が渡されていない）"
    if args.database is not None:
        if not args.database.exists():
            print(f"describe_schema: {args.database} がない", file=sys.stderr)
            return 1
        data = sqlite3.connect(f"file:{args.database}?mode=ro", uri=True)
        source = args.source or "D1 の本番データ（`wrangler d1 export`）"

    con = structure()
    text = render(con, data, source=source)
    args.output.write_text(text, encoding="utf-8")
    print(f"describe_schema: {args.output} を書き出した（{len(text.splitlines())} 行）")
    if args.html is not None:
        page = render_html(con, data, source=source)
        args.html.parent.mkdir(parents=True, exist_ok=True)
        args.html.write_text(page, encoding="utf-8")
        print(f"describe_schema: {args.html} を書き出した（{len(page) / 1024:.0f}KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
