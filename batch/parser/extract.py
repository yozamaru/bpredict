"""インラインscriptからJSONだけを読む。JavaScriptは実行しない。"""

import json
import re
from html.parser import HTMLParser

from .errors import DataUnavailable, ParseError

MAX_BYTES = 5 * 1024 * 1024


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ParseError("JSONのキーが重複")
        result[key] = value
    return result


def reject_constant(_value: str) -> object:
    raise ParseError("JSONに非有限数がある")


def json_object(body: str) -> dict[str, object]:
    try:
        value = json.loads(body, object_pairs_hook=unique_object, parse_constant=reject_constant)
    except (ValueError, RecursionError):
        raise ParseError("JSONが不正") from None
    if not isinstance(value, dict):
        raise ParseError("JSONのルートはオブジェクトでない")
    return value


class _Scripts(HTMLParser):
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
