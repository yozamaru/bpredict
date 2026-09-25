"""会場詳細ページから住所だけを読む。HTTP もDB も触らない（詳細設計 4.10）。

**収容人数はこのページに無い。** 2026-09-25 に再確認した。住所だけを抜き出し、
都道府県は住所の先頭から決める。
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser

from .errors import DataUnavailable, ParseError

#: 住所の見出しに付く class。この直後の `dd.definition-content` が住所である
_ADDRESS_HEADING = "definition-heading-address"
_CONTENT = "definition-content"

#: 47都道府県。住所の先頭に必ず現れる固定の集合であり、推定ではない
PREFECTURES = (
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県",
    "茨城県", "栃木県", "群馬県", "埼玉県", "千葉県", "東京都", "神奈川県",
    "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県", "岐阜県",
    "静岡県", "愛知県", "三重県", "滋賀県", "京都府", "大阪府", "兵庫県",
    "奈良県", "和歌山県", "鳥取県", "島根県", "岡山県", "広島県", "山口県",
    "徳島県", "香川県", "愛媛県", "高知県", "福岡県", "佐賀県", "長崎県",
    "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
)


@dataclass(frozen=True)
class ArenaAddress:
    address: str
    #: 住所の先頭から決まる都道府県。読めなければ None（推測で埋めない）
    prefecture: str | None


def prefecture_of(address: str) -> str | None:
    for name in PREFECTURES:
        if address.startswith(name):
            return name
    return None


class _AddressReader(HTMLParser):
    """住所の見出しを見つけたら、次の `dd.definition-content` の文字を集める。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[str] = []
        self._armed = False
        self._depth = 0
        self._buffer: list[str] = []

    def _classes(self, attrs: list[tuple[str, str | None]]) -> list[str]:
        for name, value in attrs:
            if name == "class":
                return (value or "").split()
        return []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = self._classes(attrs)
        if tag == "dt" and _ADDRESS_HEADING in classes:
            self._armed = True
            return
        if self._depth:
            self._depth += 1
            return
        if self._armed and tag == "dd" and _CONTENT in classes:
            self._armed = False
            self._depth = 1
            self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        if not self._depth:
            return
        self._depth -= 1
        if self._depth == 0:
            self.found.append(" ".join("".join(self._buffer).split()))

    def handle_data(self, data: str) -> None:
        if self._depth:
            self._buffer.append(data)


def parse_arena_address(body: str) -> ArenaAddress:
    """住所を1つ返す。

    `DataUnavailable`: 住所の欄がない（会場によっては載っていない）。飛ばして報告する
    `ParseError`: 住所が複数あって食い違う（構造が変わった疑い）
    """
    if type(body) is not str:
        raise ParseError("会場詳細の本文が文字列でない")
    reader = _AddressReader()
    reader.feed(body)
    reader.close()
    values = [unicodedata.normalize("NFKC", v) for v in reader.found if v]
    if not values:
        raise DataUnavailable("会場詳細に住所がない")
    if len(set(values)) > 1:
        raise ParseError("会場詳細の住所が複数あって食い違う")
    address = values[0]
    return ArenaAddress(address=address, prefecture=prefecture_of(address))
