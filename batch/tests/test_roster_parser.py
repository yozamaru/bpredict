"""登録選手一覧の解釈（詳細設計 1.2 / 4.4）。合成データだけで検証する。"""

from __future__ import annotations

import pytest

from batch.parser.errors import ParseError, ValidationError
from batch.parser.roster_parser import POSITIONS, parse_roster, split_position
from batch.scraper.client import ConfigurationError
from batch.scraper.roster import roster_url
from batch.tests.fixtures.roster import card, page

# --- URL の組み立て ---


def test_url_carries_the_season_start_year_and_official_club_id() -> None:
    url = roster_url(2026, "704")
    assert url.endswith("/roster/?year=2026&club=704")


def test_url_rejects_a_year_before_the_first_season() -> None:
    with pytest.raises(ConfigurationError):
        roster_url(2015, "704")


def test_url_rejects_a_non_numeric_club_id() -> None:
    """**クラブは公式の `TeamID` である**（詳細設計 1.2）。slug を渡させない。"""
    with pytest.raises(ConfigurationError):
        roster_url(2026, "utsunomiya-brex")


# --- ポジションと背番号の分解 ---


def test_a_single_position_is_taken_as_is() -> None:
    assert split_position("SG #8") == ("SG", "8")


def test_a_multi_position_keeps_only_the_first() -> None:
    """**運営者の判断（2026-10-05）。`C/PF` は `C` になり `PF` を捨てる。**

    実データは `C/PF` `SG/SF` を取るが、DDL は5値のいずれかに限る（詳細設計 1.2）。
    **情報を捨てることを承知のうえで DDL を変えない方を採った。**
    """
    assert split_position("C/PF #24") == ("C", "24")
    assert split_position("SG/SF #4") == ("SG", "4")


def test_whitespace_between_the_position_and_the_number_is_folded() -> None:
    """実データはポジションと背番号のあいだに改行と空白が入る。"""
    assert split_position("SG/SF\n                    #4") == ("SG", "4")


def test_an_unknown_position_is_rejected_not_nulled() -> None:
    """**黙って None にしない。**

    サイトが表記を変えたときに、ポジションが静かに全件 NULL になる。
    """
    with pytest.raises(ValidationError):
        split_position("G #8")
    with pytest.raises(ValidationError):
        split_position("フォワード #8")


def test_a_missing_number_is_allowed() -> None:
    """背番号は `player_seasons.number` が NULL 許容である（詳細設計 1.2）。"""
    assert split_position("PG") == ("PG", None)


def test_a_number_that_is_not_digits_is_rejected() -> None:
    with pytest.raises(ValidationError):
        split_position("PG #??")


def test_zero_is_a_valid_number() -> None:
    """背番号 0 は実在する。`or None` で落とさない。"""
    assert split_position("PG #0") == ("PG", "0")


def test_the_five_positions_are_the_check_constraint() -> None:
    """**DDL と2箇所で別に決めない**（詳細設計 1.2 の CHECK 制約）。"""
    assert POSITIONS == ("PG", "SG", "SF", "PF", "C")


# --- 一覧の解釈 ---


def test_every_card_becomes_one_entry() -> None:
    roster = parse_roster(page())
    assert [e.player_id for e in roster.entries] == ["9001", "9002", "9003", "9004"]
    assert [e.position for e in roster.entries] == ["SG", "C", "SG", "PG"]
    assert [e.number for e in roster.entries] == ["8", "24", "4", "0"]
    assert roster.dropped == 0


def test_entity_references_in_the_name_are_restored() -> None:
    body = page([card("9001", "架空 &amp; 選手", "PG #1")])
    assert parse_roster(body).entries[0].name == "架空 & 選手"


def test_an_unreadable_position_drops_the_player_not_the_club() -> None:
    """**1人の表記ゆれで16人を失わない**（詳細設計 4.13）。

    例外で返すと組ごと失われる。落とした件数を `dropped` で渡す。
    """
    body = page([
        card("9001", "架空選手一", "SG #8"),
        card("9002", "架空選手二", "G #9"),       # 5値のどれでもない
        card("9003", "架空選手三", "PF #10"),
    ])
    roster = parse_roster(body)
    assert [e.player_id for e in roster.entries] == ["9001", "9003"]
    assert roster.dropped == 1


def test_no_card_at_all_is_a_parse_error() -> None:
    """**実在する組を渡すのは呼び出し側の責務である**（4.13）。

    0件は構造が変わった疑いであり、黙って空を返さない。
    """
    with pytest.raises(ParseError):
        parse_roster(page([]))


def test_every_player_dropped_is_still_a_parse_error() -> None:
    """1件も読めないときは、落ちた件数の有無で分けない。"""
    with pytest.raises(ParseError):
        parse_roster(page([card("9001", "架空選手一", "G #8")]))


def test_a_card_without_the_name_element_is_a_parse_error() -> None:
    body = page([
        ('<a class="playerInfo-player" href="?PlayerID=9001">'
         '<div class="playerInfo-player-position">ポジション：<span>PG #1</span></div></a>')
    ])
    with pytest.raises(ParseError):
        parse_roster(body)


def test_a_card_without_the_position_element_is_a_parse_error() -> None:
    body = page([
        ('<a class="playerInfo-player" href="?PlayerID=9001">'
         '<div class="playerInfo-player-name">架空選手一</div></a>')
    ])
    with pytest.raises(ParseError):
        parse_roster(body)


def test_an_empty_name_is_a_parse_error() -> None:
    with pytest.raises(ParseError):
        parse_roster(page([card("9001", "   ", "PG #1")]))


def test_the_same_player_twice_is_counted_once() -> None:
    """実サイトは画像と名前で別のリンクになることがある。先に出たものを採る。"""
    body = page([card("9001", "架空選手一", "PG #1"), card("9001", "架空選手一", "PG #1")])
    assert len(parse_roster(body).entries) == 1


def test_the_image_attributes_are_not_read() -> None:
    """**写真を取得しない**（要件 5.3）ため画像の属性に触らない。

    `alt` に別の氏名、`src` に別のクラブIDを入れても結果が変わらないこと。
    """
    plain = parse_roster(page()).entries
    noisy = page([
        ('<a class="playerInfo-player" href="?PlayerID=9001">'
         '<div class="playerInfo-player-img">'
         '<img alt="別の氏名" src="https://example.invalid/roster/999/2000-01/x.png">'
         "</div>"
         '<div class="playerInfo-player-name">架空選手一</div>'
         '<div class="playerInfo-player-position">ポジション：<span>SG #8</span></div></a>')
    ])
    entry = parse_roster(noisy).entries[0]
    assert (entry.player_id, entry.name, entry.position) == ("9001", "架空選手一", "SG")
    assert plain[0].name == "架空選手一"


def test_only_four_fields_are_kept() -> None:
    """**取る項目は4つだけである**（詳細設計 1.2 / 4.4）。

    身長・生年月日・登録国籍の列を足さない — 出典を取りに行かないと決めた
    （要件 5.3）。列が増えたらここで落ちる。
    """
    entry = parse_roster(page()).entries[0]
    assert sorted(vars(entry)) == ["name", "number", "player_id", "position"]
