"""登録選手一覧（`/roster/`）の解釈（詳細設計 1.2 / 4.4）。

**埋め込みJSONを持たない。** ボックススコアと違い HTML を読む（2026-10-05 の実測で
`_contexts_*` が1つもない）。`fields.py` の対応表はボックススコアの JSON 用であり、
**出典が別なので対応表も別に持つ**。

取る項目は4つだけである — 選手ID・氏名・ポジション・背番号。
**`img` の `alt` と `src` を読まない**（写真を取得しないため属性に触らない。
クラブと季はリクエストの引数として既に分かっている）。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from batch.parser.errors import ParseError, ValidationError

#: `player_seasons.position` の CHECK 制約（詳細設計 1.2）。
POSITIONS = ("PG", "SG", "SF", "PF", "C")

#: 1クラブのカード。`playerInfo-player` のリンクから次のリンクまでを1件とみなす。
_CARD = re.compile(
    r'<a\s+class="playerInfo-player"\s+href="[^"]*?PlayerID=(?P<id>\d+)"(?P<body>.*?)</a>',
    re.DOTALL,
)
_NAME = re.compile(r'<div class="playerInfo-player-name">(?P<name>[^<]*)</div>')
_POSITION = re.compile(
    r'<div class="playerInfo-player-position">.*?<span>(?P<text>[^<]*)</span>', re.DOTALL
)


@dataclass(frozen=True)
class RosterEntry:
    """1選手の季の断面。**保持するのは4項目だけ**（詳細設計 1.2）。"""

    player_id: str
    name: str
    position: str
    number: str | None


@dataclass(frozen=True)
class Roster:
    """1クラブ・1シーズンの解析結果。

    **落とした件数を数えて返す。** 例外で返すと組ごと失われる — 設計 4.13 は
    「その選手を落として数える。組ごと落とさない」と定めている（1人の表記ゆれで
    16人を失わない）。**黙って減らさないため、件数を呼び出し側へ渡す。**
    """

    entries: list[RosterEntry]
    dropped: int = 0


def _text(raw: str) -> str:
    """HTML の実体参照を戻し、空白を1つに畳む。"""
    return re.sub(r"\s+", " ", html.unescape(raw)).strip()


def split_position(text: str) -> tuple[str, str | None]:
    """`SG #8` / `C/PF #24` を（ポジション, 背番号）に分ける。

    **複数値は先頭だけを採る**（運営者の判断。2026-10-05。詳細設計 1.2）。
    `C/PF` は `C` になり、`PF` を捨てる。**捨てていることは設計文書に書いてある。**

    **5値のどれでもない値は落とす。** 黙って None にすると、サイトが表記を
    変えたときにポジションが静かに全件 NULL になる。
    """
    cleaned = _text(text)
    if not cleaned:
        raise ParseError("ポジション欄が空である")
    position_text, _, number_text = cleaned.partition("#")
    first = position_text.strip().split("/")[0].strip().upper()
    if first not in POSITIONS:
        raise ValidationError("ポジションが想定の5値のいずれでもない")
    number = number_text.strip() or None
    if number is not None and re.fullmatch(r"\d{1,3}", number) is None:
        raise ValidationError("背番号が1〜3桁の数字でない")
    return first, number


def parse_roster(body: str) -> Roster:
    """1クラブ・1シーズンの登録選手を返す。

    **0件は `ParseError` にする。** 実在する組（`club_seasons` にある）を渡すのは
    呼び出し側の責務であり（4.13）、0件は構造が変わった疑いである。

    **1選手のポジションが読めないだけで組ごと落とさない**（4.13）。落とした件数は
    `Roster.dropped` で返す。
    """
    entries: list[RosterEntry] = []
    dropped = 0
    seen: set[str] = set()
    for card in _CARD.finditer(body):
        player_id = card.group("id")
        if player_id in seen:
            # 同じ選手が2回出る（画像と名前で別のリンクになる等）。先に出たものを採る
            continue
        seen.add(player_id)
        chunk = card.group("body")
        name = _NAME.search(chunk)
        position = _POSITION.search(chunk)
        if name is None or position is None:
            raise ParseError("選手カードに氏名またはポジションの要素がない")
        try:
            kind, number = split_position(position.group("text"))
        except ValidationError:
            dropped += 1
            continue
        label = _text(name.group("name"))
        if not label:
            raise ParseError("選手の氏名が空である")
        entries.append(
            RosterEntry(player_id=player_id, name=label, position=kind, number=number)
        )
    if not entries:
        # **1件も読めないのは構造が変わった疑いである。** 落ちた件数の有無で
        # 分けない — どちらにせよ呼び出し側は「取れなかった」として数える
        raise ParseError("登録選手が1件も見つからない")
    return Roster(entries=entries, dropped=dropped)
