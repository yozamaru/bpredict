"""架空ID・架空名による登録選手一覧の生成。HTTP取得した本文ではない。

**構造だけを模す**（要件 4.5.2）。実サイトの文言・記事・画像参照を残さない。
"""


def card(player_id: str, name: str, position: str) -> str:
    """1選手のカード。`position` は `SG #8` のような生の文字列を渡す。

    実サイトは画像と名前を同じ `<a>` に入れる。**`img` の属性は読まない**ため、
    fixture にも `alt` と `src` を置かない（読まないことをテストで固定する）。
    """
    return (
        f'<div class="grid-col"><a class="playerInfo-player"'
        f' href="https://example.invalid/roster_detail/?PlayerID={player_id}">'
        f'<div class="playerInfo-player-img"></div>'
        f'<div class="playerInfo-player-body">'
        f'<div class="playerInfo-player-name">{name}</div>'
        f'<div class="playerInfo-player-position">ポジション：'
        f"<span>{position}</span></div>"
        f"</div></a></div>"
    )


def page(cards: list[str] | None = None) -> str:
    """一覧ページ。`select` は実サイトと同じ名前だけを持たせる。"""
    if cards is None:
        cards = [
            card("9001", "架空選手一", "SG #8"),
            card("9002", "架空選手二", "C/PF #24"),
            card("9003", "架空選手三", "SG/SF\n                    #4"),
            card("9004", "架空選手四", "PG #0"),
        ]
    options = "".join(f'<option value="{y}">{y}</option>' for y in (2026, 2025))
    clubs = "".join(f'<option value="{c}">架空{c}</option>' for c in (704, 703))
    return (
        "<!DOCTYPE html><html><body>"
        f'<select name="year">{options}</select>'
        f'<select name="club"><option value=""></option>{clubs}</select>'
        '<div class="grid">' + "".join(cards) + "</div>"
        "</body></html>"
    )
