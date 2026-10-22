"""インラインscriptからJSONだけを読む。JavaScriptは実行しない。"""

import json
import re
from html.parser import HTMLParser

from .errors import DataUnavailable, ParseError

MAX_BYTES = 5 * 1024 * 1024


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """JSON の重複キーを落とす（`json.loads` の `object_pairs_hook`）。

    **既定の `json` は重複を黙って受け、最後の値を採る。** 対応表で特定の
    フィールドを抜き出す設計（詳細設計 4.4）では、同じキーが2つある本文から
    どちらを読んだのか分からなくなる。**気づける方に倒す。**
    """
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ParseError("JSONのキーが重複")
        result[key] = value
    return result


def reject_constant(_value: str) -> object:
    """`NaN` / `Infinity` / `-Infinity` を落とす（`json.loads` の `parse_constant`）。

    **この3つは JSON ではない。** 規格に該当するリテラルが無く、Python の `json` が
    拡張として受けているだけである。したがって本文にあること自体が
    「JSON ではないものを読んでいる」という信号であり、値として運ぶより止める。

    **一番外側で止める理由は、網羅できるからである。** `normalize.integer` も
    非有限数を落とすが（`math.isfinite`）、それはカウント値として読む列だけを守る。
    ここで落とせば、文字列・旗・日時として読む列と、**これから足す列**にも同じ
    保証が効く。
    """
    raise ParseError("JSONに非有限数がある")


def json_object(body: str) -> dict[str, object]:
    """本文全体を JSON オブジェクトとして読む。

    重複キーと非有限数を落とす設定を**呼び出し側に選ばせない** — ここを通る限り、
    上の2つの関門は必ず効く。ルートが配列やリテラルなら落とす。
    """
    try:
        value = json.loads(body, object_pairs_hook=unique_object, parse_constant=reject_constant)
    except (ValueError, RecursionError):
        raise ParseError("JSONが不正") from None
    if not isinstance(value, dict):
        raise ParseError("JSONのルートはオブジェクトでない")
    return value


class _Scripts(HTMLParser):
    """インライン `<script>` の中身だけを集める。

    ボックススコアは HTML の表ではなく `<script>` 内の埋め込み JSON で配信される
    （基本設計 2.1。Phase 0 で確認）。**`src` 付きの `<script>` は飛ばす** —
    外部ファイルの取得はしない（取得は `scraper/` の責務であり、ここは解釈だけを
    持つ）。

    **`convert_charrefs=False` が要る。** 既定の True だと `&amp;` などが
    HTML 実体参照として展開されるが、**`<script>` の中身は HTML エスケープされて
    いない**ため、展開すると JSON 本文が書き換わって壊れる。

    `handle_starttag` / `handle_data` / `handle_endtag` は `HTMLParser` が呼ぶ
    約束であり、こちらから呼ばない。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.scripts: list[str] = []
        self.current: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and "src" not in dict(attrs):
            self.current = []

    def handle_data(self, data: str) -> None:
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.current is not None:
            self.scripts.append("".join(self.current))
            self.current = None


def extract_embedded_json(body: str) -> dict[str, object]:
    """試合データの埋め込み JSON を取り出す。**JavaScript は実行しない。**

    出典は `<script>` 内の `_contexts_s3id.data = {...}` で、代入の右辺だけを
    JSON として読む（詳細設計 4.4）。DOM を辿るより壊れにくい — クラス名や
    レイアウトの変更に影響されない。

    次の4つで落とす。**どれも「黙って1つ選ぶ」ことを避けるためにある。**

    - 本文が5MBを超える（要件 5.2 のレスポンスサイズ上限をここでも効かせる）
    - 埋め込み位置が**2つ以上**ある（どちらが本物か決められない）
    - 代入の後ろに `;` 以外の式が続く（途中で読み終えた疑い）
    - 値がオブジェクトでない

    **`DataUnavailable` は「構造が変わった」ではない。** `_contexts.scheduleKey`
    はあるのに `data` が無い（または `null`）場合で、**ボックススコアが未公開の
    終了済み試合**にあたる（2017-18 の `1810` で初めて出た。詳細設計 9章）。
    呼び出し側はその試合だけを飛ばし、構造変更として扱わない。
    """
    if len(body.encode("utf-8")) > MAX_BYTES:
        raise ParseError("レスポンスがサイズ上限を超える")
    parser = _Scripts()
    parser.feed(body)
    parser.close()
    pattern = re.compile(r"(?:^|[;\r\n])\s*(?:window\.)?_contexts_s3id\.data\s*=\s*")
    candidates = [(script, m.end()) for script in parser.scripts for m in pattern.finditer(script)]
    if not candidates:
        if any(re.search(r"_contexts\.scheduleKey\s*=", s) for s in parser.scripts):
            raise DataUnavailable("終了済み試合データが未公開")
        raise ParseError("試合データの埋め込み位置がない")
    if len(candidates) != 1:
        raise ParseError("試合データの埋め込みが重複")
    script, start = candidates[0]
    decoder = json.JSONDecoder(object_pairs_hook=unique_object, parse_constant=reject_constant)
    try:
        value, end = decoder.raw_decode(script[start:])
    except (ValueError, RecursionError):
        raise ParseError("埋め込みJSONが不正") from None
    tail = script[start + end:].lstrip()
    if tail and not tail.startswith(";"):
        raise ParseError("JSONの後ろに未対応の式がある")
    if value is None:
        raise DataUnavailable("終了済み試合データが未公開")
    if not isinstance(value, dict):
        raise ParseError("試合データがオブジェクトでない")
    return value
