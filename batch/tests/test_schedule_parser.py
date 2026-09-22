"""Synthetic schedule markup: no downloaded text, names, IDs, or media URLs."""

from __future__ import annotations

import json

import pytest

from batch.parser.errors import ParseError, ValidationError
from batch.parser.schedule_parser import parse_club_options, parse_schedule

CLUBS = {"架空ホーム": "club-home", "架空アウェイ": "club-away"}
HEADER = '<div class="champion-box box-container"><span class="title">2026.10.01(木)</span></div>'


def game_html(
    game_id: str = "game-demo-1", *, state: str = "見どころ", home: str = "", away: str = "",
    clock: str = "19:05", name: str = "架空ホーム",
) -> str:
    # The unrelated nested list-item is deliberate: counting every li.list-item
    # used to make a single scheduled game appear to contain several games.
    return f"""
    <li class="list-item" id="{game_id}"><div class="inner">
      <a class="data-game click_schedule_highlights"
         href="/game_detail/?ScheduleKey={game_id}&amp;tab=1">
        <div class="game">
          <span class="team home"><span class="team-name">{name}</span></span>
          <span class="point">
            <span class="number home-score"><span>{home}</span></span>
            <span class="number away-score"><span>{away}</span></span>
          </span>
          <span class="team away"><span class="team-name">架空アウェイ</span></span>
        </div>
        <div class="info"><div class="info-arena">
          <span>第1節</span><span>架空地域 | 架空会場</span><span>{clock}</span>
        </div><div class="info-scorestate"><span>{state}</span></div></div>
      </a>
      <div class="data-link"><ul><li class="list-item item-media">
        <a href="/unrelated/">dummy</a>
      </li></ul></div>
    </div></li>
    """


def body(*topics: str, index: int | None = 20) -> str:
    return json.dumps({"topics": list(topics), "broadcasts": [], "index": index})


def test_parse_club_selector_ignores_other_selectors_and_empty_option():
    html = """
      <select name="year"><option value="2026">2026</option></select>
      <select name="club">
        <option value="">dummy all</option>
        <option value="club-home"> 架空ホーム </option>
        <option value="club-away">架空アウェイ</option>
      </select>
    """
    assert parse_club_options(html) == CLUBS


@pytest.mark.parametrize("html", [
    "<html></html>",
    '<select name="club"><option value="">dummy all</option></select>',
    '<select name="club"><option>架空ホーム</option></select>',
    '<select name="club"><option value="club-home"></option></select>',
    ('<select name="club"><option value="club-a">架空</option>'
     '<option value="club-b">架空</option></select>'),
    ('<select name="club"><option value="club-a">架空A</option>'
     '<option value="club-a">架空B</option></select>'),
])
def test_invalid_club_selector_fails(html):
    with pytest.raises(ParseError):
        parse_club_options(html)


def test_scheduled_game_uses_explicit_clubs_jst_and_ignores_media_rows():
    page = parse_schedule(body(HEADER + game_html()), year=2026, event=2, clubs_by_name=CLUBS)
    assert page.next_index == 20
    assert page.last_date == "2026-10-01"
    assert len(page.games) == 1
    game = page.games[0]
    assert (game.game_id, game.competition, game.status) == ("game-demo-1", "REGULAR", "SCHEDULED")
    assert (game.home_source_id, game.away_source_id) == ("club-home", "club-away")
    assert (game.home_name, game.away_name) == ("架空ホーム", "架空アウェイ")
    assert (game.game_date, game.tipoff_at) == ("2026-10-01", "2026-10-01T10:05:00Z")
    assert (game.home_score, game.away_score) == (None, None)
    assert game.source_url == "https://www.bleague.jp/game_detail/?ScheduleKey=game-demo-1&tab=2"


def test_one_topic_can_contain_multiple_games_and_date_headers():
    second_header = HEADER.replace("2026.10.01(木)", "2026.10.02(金)")
    page = parse_schedule(body(HEADER + game_html() + second_header + game_html("game-demo-2")),
                          year=2026, event=2, clubs_by_name=CLUBS)
    assert [game.game_date for game in page.games] == ["2026-10-01", "2026-10-02"]
    assert page.last_date == "2026-10-02"


def test_finished_playoff_preserves_zero_and_normalizes_fullwidth_numbers():
    page = parse_schedule(body(HEADER, game_html(state="FINAL", home="０", away="１０２")),
                          year=2026, event=3, clubs_by_name=CLUBS)
    game = page.games[0]
    assert (game.competition, game.status) == ("PLAYOFF", "FINISHED")
    assert (game.home_score, game.away_score) == (0, 102)


def test_date_carryover_and_explicit_header_override():
    previous = parse_schedule(body(game_html(), index=40), year=2026, event=2, clubs_by_name=CLUBS,
                              previous_date="2026-10-03", index=20)
    assert previous.games[0].game_date == "2026-10-03"
    page = parse_schedule(body(HEADER, game_html()), year=2026, event=2, clubs_by_name=CLUBS,
                          previous_date=previous.last_date)
    assert page.games[0].game_date == "2026-10-01"


def test_empty_terminal_retains_last_date():
    page = parse_schedule(body(index=None), year=2026, event=2, clubs_by_name=CLUBS,
                          previous_date="2026-10-03", index=40)
    assert page.games == ()
    assert page.next_index is None
    assert page.last_date == "2026-10-03"


@pytest.mark.parametrize("clock", ["", "未定", "調整中", "--:--", "-"])
def test_unpublished_time_does_not_invent_tipoff(clock):
    page = parse_schedule(body(HEADER, game_html(clock=clock)), year=2026, event=2, clubs_by_name=CLUBS)
    assert page.games[0].tipoff_at is None


def test_missing_time_span_does_not_treat_venue_as_time():
    html = game_html().replace("<span>19:05</span>", "")
    page = parse_schedule(body(HEADER, html), year=2026, event=2, clubs_by_name=CLUBS)
    assert page.games[0].tipoff_at is None


def test_jst_game_date_is_preserved_when_utc_is_previous_day():
    page = parse_schedule(body(HEADER, game_html(clock="00:30")), year=2026, event=2, clubs_by_name=CLUBS)
    assert page.games[0].game_date == "2026-10-01"
    assert page.games[0].tipoff_at == "2026-09-30T15:30:00Z"


def test_same_duplicate_is_emitted_once_and_conflict_rejects_page():
    page = parse_schedule(body(HEADER, game_html(), game_html()), year=2026, event=2, clubs_by_name=CLUBS)
    assert len(page.games) == 1
    with pytest.raises(ParseError, match="conflicting duplicate"):
        parse_schedule(body(HEADER, game_html(), game_html(clock="20:05")),
                       year=2026, event=2, clubs_by_name=CLUBS)


@pytest.mark.parametrize("state, expected", [
    ("延期", "POSTPONED"), ("試合延期", "POSTPONED"),
    ("中止", "CANCELLED"), ("試合中止", "CANCELLED"),
])
def test_explicit_postponed_cancelled_states(state, expected):
    page = parse_schedule(body(HEADER, game_html(state=state)), year=2026, event=2, clubs_by_name=CLUBS)
    assert page.games[0].status == expected


@pytest.mark.parametrize("state", ["LIVE", "3Q", "", "unknown", "見どころ LIVE"])
def test_unknown_or_live_state_never_becomes_scheduled_or_finished(state):
    with pytest.raises(ParseError, match="state"):
        parse_schedule(body(HEADER, game_html(state=state)), year=2026, event=2, clubs_by_name=CLUBS)


@pytest.mark.parametrize("malformed", [
    "<html>dummy failure</html>", "[]", "null", "{}",
    '{"topics":[],"index":null,"index":20}',
    '{"topics":null,"index":20}', '{"topics":[{}],"index":20}',
    '{"topics":[],"index":20}',
    body("<div>dummy changed layout</div>"), body(HEADER), body(""),
])
def test_invalid_page_is_not_silent_empty_success(malformed):
    with pytest.raises(ParseError):
        parse_schedule(malformed, year=2026, event=2, clubs_by_name=CLUBS)


@pytest.mark.parametrize("next_index", [0, 10, 20, -1, True, "40", 40.5])
def test_continuation_must_be_integer_and_advance(next_index):
    malformed = json.dumps({"topics": [HEADER, game_html()], "index": next_index})
    with pytest.raises(ParseError, match="index"):
        parse_schedule(malformed, year=2026, event=2, clubs_by_name=CLUBS, index=20)


@pytest.mark.parametrize("event", [1, 4, 5, 11, 20, True, "2"])
def test_excluded_or_mistyped_competition_is_rejected(event):
    with pytest.raises(ValidationError, match="competition"):
        parse_schedule(body(HEADER, game_html()), year=2026, event=event, clubs_by_name=CLUBS)


def test_year_boundary_within_season_is_valid():
    header = HEADER.replace("2026.10.01(木)", "2027.01.03(日)")
    page = parse_schedule(body(header, game_html()), year=2026, event=2, clubs_by_name=CLUBS)
    assert page.games[0].game_date == "2027-01-03"


@pytest.mark.parametrize("header", ["2026.02.30(月)", "2028.01.01(土)"])
def test_invalid_calendar_date_and_wrong_season_are_rejected(header):
    with pytest.raises(ValidationError):
        parse_schedule(body(HEADER.replace("2026.10.01(木)", header), game_html()),
                       year=2026, event=2, clubs_by_name=CLUBS)


@pytest.mark.parametrize("previous_date", ["20260230", "20261001", "2026-13-01", "2024-10-01"])
def test_invalid_previous_date_is_rejected(previous_date):
    with pytest.raises(ValidationError):
        parse_schedule(body(game_html()), year=2026, event=2, clubs_by_name=CLUBS,
                       previous_date=previous_date)


def test_rows_without_a_date_are_skipped_not_guessed():
    """日付が確定しない区画の行は飛ばす。**前の日付を引き継がせない。**

    引き継ぐと、非リーグ戦（オールスター等）に誤った日付が付く。
    """
    page = parse_schedule(body(game_html()), year=2026, event=2, clubs_by_name=CLUBS)
    assert page.games == ()
    assert page.skipped == 1


@pytest.mark.parametrize("clock", ["24:00", "19:60"])
def test_out_of_range_time_is_rejected(clock):
    with pytest.raises(ValidationError, match="time"):
        parse_schedule(body(HEADER, game_html(clock=clock)), year=2026, event=2, clubs_by_name=CLUBS)


@pytest.mark.parametrize("home, away, state, error", [
    ("251", "100", "FINAL", ValidationError),
    ("9" * 5000, "100", "FINAL", ValidationError),
    ("100", "100", "FINAL", ValidationError),
    ("", "100", "FINAL", ParseError),
    ("-", "100", "FINAL", ParseError),
    ("ten", "100", "FINAL", ParseError),
    ("0", "0", "見どころ", ValidationError),
    ("10", "20", "試合中止", ValidationError),
])
def test_invalid_scores_and_status_consistency(home, away, state, error):
    with pytest.raises(error):
        parse_schedule(body(HEADER, game_html(home=home, away=away, state=state)),
                       year=2026, event=2, clubs_by_name=CLUBS)


def test_unknown_club_is_skipped_not_guessed():
    """その年度のクラブ一覧にない相手の試合は飛ばす（名前からIDを推測しない）。

    `event=2`（リーグ戦）に選抜チームや海外クラブが混ざる実データがある
    （2016-17 の `B.BLACK` 対 `B.WHITE`、`川崎` 対 `安養KGC`）。
    改称は選択肢側も当該年度の名称になるため、取りこぼしにはならない。
    """
    page = parse_schedule(body(HEADER, game_html(name="別の架空クラブ")),
                          year=2026, event=2, clubs_by_name=CLUBS)
    assert page.games == ()
    assert page.skipped == 1


def test_non_league_block_is_skipped_but_real_games_are_kept():
    """非リーグ戦の区画を飛ばしても、同じページの実試合は取り込むこと。"""
    special = ('<div class="champion-box box-container">'
               '<span class="title">オールスター</span></div>')
    page = parse_schedule(
        body(HEADER, game_html("game-real"), special, game_html("game-star", name="B.BLACK")),
        year=2026, event=2, clubs_by_name=CLUBS,
    )
    assert [game.game_id for game in page.games] == ["game-real"]
    assert page.skipped == 1


def stage_row(game_id: str = "game-cs-1", *, day: str = "05/13 (土)", clock: str = "16:05") -> str:
    """ステージ名の区画の行。**日付の span が1つ余分に入る**（実データの形）。"""
    return f"""
    <li class="list-item" id="{game_id}"><div class="inner">
      <a class="data-game click_schedule_report"
         href="/game_detail/?ScheduleKey={game_id}&amp;tab=1">
        <div class="game">
          <span class="team home"><span class="team-name">架空ホーム</span></span>
          <span class="point">
            <span class="number home-score"><span>89</span></span>
            <span class="number away-score"><span>75</span></span>
          </span>
          <span class="team away"><span class="team-name">架空アウェイ</span></span>
        </div>
        <div class="info"><div class="info-arena">
          <span>クォーターファイナル</span><span>架空地域 | 架空会場</span>
          <span>{day}</span><span>{clock}</span>
        </div><div class="info-scorestate"><span>FINAL</span></div></div>
      </a>
    </div></li>
    """


STAGE_HEADING = ('<div class="champion-box box-container">'
                 '<span class="title">B.LEAGUE CHAMPIONSHIP 2026-27</span></div>')


def test_stage_block_takes_the_date_from_the_row():
    """見出しがステージ名の区画では、**行の日付を使う**。

    2016-17 のチャンピオンシップは `B.LEAGUE CHAMPIONSHIP 2016-17` という見出しで、
    行の時刻欄に `05/13 (土)16:05` と日付が入っていた。見出しだけを見る実装は
    **CS 15試合を丸ごと捨てていた**。
    """
    page = parse_schedule(body(STAGE_HEADING, stage_row(), index=None),
                          year=2026, event=3, clubs_by_name=CLUBS, index=0)
    assert page.skipped == 0
    assert len(page.games) == 1
    game = page.games[0]
    # 1〜8月はシーズン開始年の翌年
    assert game.game_date == "2027-05-13"
    assert game.tipoff_at == "2027-05-13T07:05:00Z"
    assert game.competition == "PLAYOFF"
    assert (game.home_score, game.away_score) == (89, 75)


def test_stage_block_infers_the_calendar_year_from_the_month():
    """9〜12月は開始年、1〜8月は翌年（シーズンの並び）。"""
    page = parse_schedule(body(STAGE_HEADING, stage_row(day="10/05 (土)"), index=None),
                          year=2026, event=3, clubs_by_name=CLUBS, index=0)
    assert page.games[0].game_date == "2026-10-05"


def test_row_without_any_date_is_skipped():
    """見出しも行も日付を持たない場合は飛ばす（推測で埋めない）。"""
    page = parse_schedule(body(STAGE_HEADING, game_html(), index=None),
                          year=2026, event=2, clubs_by_name=CLUBS)
    assert page.games == ()
    assert page.skipped == 1


def test_terminal_page_with_games_is_accepted():
    """`index=null` でも試合があれば最終ページとして扱うこと。

    2016-17 のチャンピオンシップは 15試合 / `index=null` の単一ページで、
    進行を必須にしていた実装は**CSを1件も取り込めなかった**。
    """
    page = parse_schedule(body(HEADER, game_html(), index=None), year=2026, event=3,
                          clubs_by_name=CLUBS, index=0)
    assert len(page.games) == 1
    assert page.next_index is None
    assert page.games[0].competition == "PLAYOFF"


def test_same_official_club_on_both_sides_is_rejected():
    clubs = {"架空ホーム": "club-same", "架空アウェイ": "club-same"}
    with pytest.raises(ValidationError, match="teams"):
        parse_schedule(body(HEADER, game_html()), year=2026, event=2, clubs_by_name=clubs)


@pytest.mark.parametrize("old, new", [
    ('id="game-demo-1"', ''),
    ("ScheduleKey=game-demo-1", "ScheduleKey=another-game"),
    ("/game_detail/?", "https://example.invalid/game_detail/?"),
    ("ScheduleKey=game-demo-1", "ScheduleKey=game-demo-1&amp;ScheduleKey=game-demo-1"),
    ("ScheduleKey=game-demo-1", "ScheduleKey=game-demo-1&amp;ScheduleKey="),
    ('class="info-scorestate"', 'class="changed-state"'),
    ('class="number home-score"', 'class="changed-score"'),
])
def test_structural_or_identifier_drift_is_rejected(old, new):
    with pytest.raises(ParseError):
        parse_schedule(body(HEADER, game_html().replace(old, new)),
                       year=2026, event=2, clubs_by_name=CLUBS)
