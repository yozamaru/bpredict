"""`docs/manuals/*.md` を PDF にする（生成物。手で編集しない）。

**PDF エンジンを追加で入れない。** `pandoc` で HTML にし、**Chrome の
ヘッドレス印刷**で PDF にする。手元に `pdflatex` / `wkhtmltopdf` /
`weasyprint` が無く、CLAUDE.md「依存を追加するとき」に照らすと
3 冊の PDF のために TeX を入れる理由が弱い。

**日本語はシステムフォントで組む。** `docs/manuals/style.css` が
明朝・サンセリフ・等幅の 3 系統を指定する（要件 8.1 と同じ方針で、
Web フォントを読み込まない）。

使い方:
    python3 scripts/build_manuals.py [--out <dir>]
"""

from __future__ import annotations

import argparse
import html
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = ROOT / "docs" / "manuals"
CSS = SOURCE_DIR / "style.css"
CHROME = Path(
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
)

#: 作る順。ファイル名がそのまま PDF の名前になる
DOCUMENTS = ("guide-user", "guide-ops", "api-spec")

#: 表紙に必ず出す。**非公式である旨は全ページの共通フッターと同じ趣旨で、
#: 1 枚目から読めるようにする**（要件 4.5.5）
DISCLAIMER = (
    "データ出典: B.LEAGUE 公式サイト（https://www.bleague.jp/）で公表されている"
    "試合日程・記録を基に、本サイトが独自に集計・加工したものです。<br>"
    "本サイトは公益社団法人ジャパン・プロフェッショナル・バスケットボールリーグ"
    "（B.LEAGUE）および各クラブとは一切関係のない、個人が運営する非公式サービスです。"
)


class BuildError(RuntimeError):
    """組み立てられない。黙って空の PDF を作らない。"""


def front_matter(text: str) -> tuple[dict[str, str], str]:
    """先頭の YAML 風ヘッダを読み、本文と分けて返す。

    **`pandoc` のメタデータ機構を使わない。** 表紙はこちらで組むため、
    `title` / `subtitle` / `version` の 3 つを読めれば足りる。
    """
    if not text.startswith("---\n"):
        raise BuildError("先頭にヘッダがない")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise BuildError("ヘッダが閉じていない")
    meta: dict[str, str] = {}
    for line in text[4:end].split("\n"):
        if not line.strip():
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    for required in ("title", "subtitle", "version"):
        if required not in meta:
            raise BuildError(f"ヘッダに {required} がない")
    return meta, text[end + 5 :]


def to_html(markdown: str) -> str:
    """`pandoc` で本文を HTML にする。**JavaScript を含めない。**"""
    if shutil.which("pandoc") is None:
        raise BuildError("pandoc がない")
    done = subprocess.run(
        ["pandoc", "--from=gfm", "--to=html5", "--wrap=none"],
        input=markdown, capture_output=True, text=True, check=False,
    )
    if done.returncode != 0:
        raise BuildError("pandoc が失敗した")
    return done.stdout


def page(meta: dict[str, str], body: str) -> str:
    """表紙と本文を 1 枚の HTML にまとめる。"""
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<title>{html.escape(meta["title"])}</title>
<style>{CSS.read_text(encoding="utf-8")}</style>
</head><body>
<div class="cover">
  <h1>{html.escape(meta["title"])}</h1>
  <div class="sub">{html.escape(meta["subtitle"])}</div>
  <div class="meta">{html.escape(meta["version"])}</div>
  <div class="note">{DISCLAIMER}</div>
</div>
{body}
</body></html>"""


def print_pdf(source: Path, target: Path) -> None:
    """Chrome のヘッドレス印刷で PDF にする。

    **`--no-pdf-header-footer` を付ける。** 既定ではファイルの URL と日付が
    各ページに刷られ、**手元の絶対パスが PDF に残る**（絶対ルール4 の趣旨）。
    """
    if not CHROME.exists():
        raise BuildError("Chrome がない")
    done = subprocess.run(
        [str(CHROME), "--headless", "--disable-gpu", "--no-pdf-header-footer",
         f"--print-to-pdf={target}", source.as_uri()],
        capture_output=True, text=True, check=False,
    )
    if not target.exists() or target.stat().st_size == 0:
        raise BuildError(f"PDF が作られなかった: {done.returncode}")
    if not target.read_bytes().startswith(b"%PDF-"):
        raise BuildError("PDF ではないものが出た")


def build(name: str, out_dir: Path) -> tuple[Path, int]:
    """1 冊作る。**ページ数も返す**（空の PDF を成功と呼ばないため）。"""
    source = SOURCE_DIR / f"{name}.md"
    if not source.exists():
        raise BuildError(f"{source.name} がない")
    meta, markdown = front_matter(source.read_text(encoding="utf-8"))
    document = page(meta, to_html(markdown))
    staging = out_dir / f"{name}.html"
    staging.write_text(document, encoding="utf-8")
    target = out_dir / f"{name}.pdf"
    print_pdf(staging, target)
    staging.unlink()
    data = target.read_bytes()
    pages = len(re.findall(rb"/Type\s*/Page[^s]", data))
    if pages < 1:
        raise BuildError("ページが 1 枚もない")
    return target, pages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="説明書を PDF にする")
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "manuals" / "pdf")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    try:
        for name in DOCUMENTS:
            target, pages = build(name, args.out)
            size = target.stat().st_size / 1024
            print(f"  {target.relative_to(ROOT)}  {pages} ページ / {size:,.0f} KB")
    except BuildError as error:
        print(f"build_manuals: 失敗（{error}）", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
