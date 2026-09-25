"""利用規約ページの節ごとの指紋（詳細設計 4.3）。

**関門が「形骸化した通知」になるのを防ぐための仕組みである。**

規約の関門はページ全体の可視テキストをハッシュしている。規約の本文が変わって
いなくても、ナビゲーション文言やバナーなど**ページのどこかが変われば止まる**。
止まるたびに中身を確認せず承認するようになれば、関門は意味を失う。

そこで節ごとのハッシュを併せて持ち、**どの節が変わったかを機械的に指せる**ようにする。
全体のハッシュは関門として残す（厳しさを落とさない）。

**リポジトリにサイトの文言を置かない**（要件 4.5.2）。指紋は見出しも含めてハッシュと
連番だけで持ち、報告に使う見出しは**実行時の生きたページから取る**。保存するのは
内容ではなくハッシュである。

節への分割は**レスポンス本文の解釈**であり、`scraper/` ではなくここに置く
（CLAUDE.md「責務の分離」）。ページ構成が変わったときの修正もここだけで済む。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from batch.scraper.client import TermsReporter, visible_text

FINGERPRINT_PATH = Path(__file__).with_name("terms_fingerprint.json")

#: 最初の見出しより前（ヘッダ・ナビゲーション）。変動しやすいのはここである
BEFORE_HEADINGS = "(見出しの前)"

_HEADING = re.compile(r"<h([1-6])\b[^>]*>(.*?)</h\1>", re.DOTALL | re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")


class FingerprintError(RuntimeError):
    """指紋のファイルが無い、壊れている、または承認済みハッシュと食い違う。"""


@dataclass(frozen=True)
class Section:
    index: int
    #: 実行時にだけ持つ。**保存しない**
    heading: str
    sha256: str


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sections(html: str) -> list[Section]:
    """見出しを境界にページを節へ分ける。

    見出しの直前までを1つ目の節（`BEFORE_HEADINGS`）とし、以降は見出しごとに
    「その見出し＋次の見出しの直前まで」を1節とする。各節は可視テキストで
    ハッシュする（マークアップの変化で止めないため。`visible_text`）。
    """
    matches = list(_HEADING.finditer(html))
    bounds = [0, *(m.start() for m in matches), len(html)]
    out: list[Section] = []
    for index in range(len(bounds) - 1):
        chunk = html[bounds[index] : bounds[index + 1]]
        text = visible_text(chunk)
        if index == 0:
            heading = BEFORE_HEADINGS
        else:
            heading = " ".join(_TAG.sub(" ", matches[index - 1].group(2)).split())
        out.append(Section(index=index, heading=heading, sha256=_digest(text)))
    return out


def build_fingerprint(html: str, approved_visible_text_sha256: str) -> dict[str, object]:
    """保存する形。**見出しの文言を含めない。**"""
    return {
        "note": (
            "利用規約ページの節ごとのハッシュ。関門が止まったとき、どの節が変わったかを"
            "指すために使う。サイトの文言は保存しない（要件 4.5.2）。"
            "承認は運営者が GitHub Variables の SCRAPER_TERMS_SHA256 を更新して行う。"
        ),
        "visibleTextSha256": approved_visible_text_sha256,
        "sections": [{"index": s.index, "sha256": s.sha256} for s in sections(html)],
    }


def load_fingerprint(path: Path | None = None) -> dict[str, object]:
    # **既定値を引数に書かない。** 引数の既定は import 時に束縛されるため、
    # `FINGERPRINT_PATH` を差し替えても効かない（テストで実際に空振りした）
    path = FINGERPRINT_PATH if path is None else path
    if not path.exists():
        raise FingerprintError(f"{path.name} がない")
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        raise FingerprintError(f"{path.name} が JSON として読めない") from None
    if not isinstance(loaded, dict) or not isinstance(loaded.get("sections"), list):
        raise FingerprintError(f"{path.name} の形が不正")
    return loaded


def _saved_sections(saved: dict[str, object]) -> dict[int, str]:
    """保存された節を `{連番: ハッシュ}` に落とす。壊れていれば止める。"""
    rows = saved.get("sections")
    if not isinstance(rows, list):
        raise FingerprintError("指紋に sections がない")
    out: dict[int, str] = {}
    for row in rows:
        if not isinstance(row, dict) or "index" not in row or "sha256" not in row:
            raise FingerprintError("指紋の節の形が不正")
        out[int(str(row["index"]))] = str(row["sha256"])
    return out


def describe_change(
    html: str,
    approved_visible_text_sha256: str,
    path: Path | None = None,
) -> list[str]:
    """どの節が変わったかを1行1件で返す。見出しは**生きたページから**取る。

    指紋が承認済みハッシュと食い違う場合は、節の比較をしない。古い指紋と比べると
    「変わっていない節が変わった」と出て、判断を誤らせる。
    """
    path = FINGERPRINT_PATH if path is None else path
    saved = load_fingerprint(path)
    if saved.get("visibleTextSha256") != approved_visible_text_sha256:
        return [
            (
                f"{path.name} が承認済みハッシュと食い違う（指紋が古い）。"
                "どの節が変わったかは判定できない"
            ),
        ]

    current = sections(html)
    before = _saved_sections(saved)
    lines: list[str] = []
    if len(current) != len(before):
        lines.append(f"節の数が変わった: {len(before)} → {len(current)}（構成の変更）")
    for section in current:
        known = before.get(section.index)
        if known is None:
            lines.append(f"第{section.index}節「{section.heading}」が増えた")
        elif known != section.sha256:
            lines.append(f"第{section.index}節「{section.heading}」が変わった")
    missing = sorted(set(before) - {s.index for s in current})
    for index in missing:
        lines.append(f"第{index}節が無くなった")
    if not lines:
        lines.append(
            "節ごとの比較では差がない。節の境界の外（見出しの入れ替えなど）が"
            "変わった可能性がある"
        )
    return lines


def report_terms_change(approved_visible_text_sha256: str) -> TermsReporter:
    """`verify_policy(terms_reporter=...)` に渡す報告関数を作る。

    **止まった理由を運用者がその場で読めるようにする**のが目的である。
    指紋が読めない場合も黙らず、読めなかったことを出す。
    """

    def report(html: str) -> None:
        print("利用規約が変わっている。どの節が変わったか:")
        try:
            for line in describe_change(html, approved_visible_text_sha256):
                print(f"  - {line}")
        except FingerprintError as error:
            print(f"  - 節ごとの比較ができない（{error}）")

    return report
