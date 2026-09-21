import copy
from dataclasses import asdict

import pytest

from batch.parser.boxscore_parser import parse_boxscore
from batch.parser.errors import (
    DataUnavailable,
    ParseError,
    ParseErrorStreak,
    ParseFailureTracker,
    ValidationError,
)
from batch.parser.extract import extract_embedded_json
from batch.parser.normalize import integer, iso_utc, minutes, timestamp
from batch.tests.fixtures.boxscore import boxscore_data, page

CLUBS = {"101": "c-home", "102": "c-away"}


def parse(data=None, **kwargs):
    return parse_boxscore(page(data), event=2, clubs=CLUBS,
                          expected_game_id="demo-game", **kwargs)


def test_extract_and_normalize_full_boxscore():
    result = parse()
    assert len(result.players) == 4
    assert len(result.teams) == 2
    assert result.game.venue_id == "901"
    assert result.game.home_club_id == "c-home"
    assert result.game.attendance == 1000
    assert result.players[0].minutes == 30.25
    assert result.players[0].started == 1
    assert result.players[0].plus_minus == -2
    assert result.teams[0].stats.oreb == 7  # Category=2の3を二重加算しない
    assert result.teams[0].stats.pts == 80
    assert result.teams[0].possessions == pytest.approx(65.4)
    assert "unused" not in repr(asdict(result))
    assert "破棄するデータ" not in repr(result)


def test_jst_date_and_estimated_end_time():
    result = parse()
    assert result.game.tipoff_at == "2025-10-01T15:05:00Z"
    assert result.game.game_date == "2025-10-02"
    assert result.game.finished_at == "2025-10-01T17:05:00Z"
    assert result.game.finished_at_is_estimated == 1


def test_measured_end_time_and_old_style_totals():
    data = boxscore_data()
    data["Game"]["GameEndTime"] = "1759338000"
    for side, tid in (("Home", 101), ("Away", 102)):
        data[f"{side}Boxscores"] = [r for r in data[f"{side}Boxscores"] if r["Category"] != 2]
        for row in data[f"{side}Boxscores"]:
            row.pop("PLUSMINUS", None)
            row["TeamID"] = tid
    result = parse(data)
    assert result.game.finished_at_is_estimated == 0
    assert result.game.finished_at == "2025-10-01T17:00:00Z"
    assert all(p.plus_minus is None for p in result.players)


def test_missing_counts_and_dnp_remain_none():
    data = boxscore_data()
    row = data["HomeBoxscores"][1]
    row["FOULON"] = ""
    row["PLUSMINUS"] = ""
    row["PlayTime"] = "DNP"
    result = parse(data)
    assert result.players[0].stats.fd is None
    assert result.players[0].minutes is None
    assert result.players[0].plus_minus is None


def test_null_stats_propagate_to_possessions():
    data = boxscore_data()
    data["HomeBoxscores"][-1]["PT2A"] = None
    result = parse(data)
    assert result.teams[0].stats.fg2a is None
    assert result.teams[0].possessions is None


@pytest.mark.parametrize("field,value", [
    ("Point", 999), ("Point", 41), ("PT2M", 21), ("FOUL", 7),
    ("RB_TOT", 999), ("RB_TOT", 9), ("PlayTime", "60:01"),
    ("PLUSMINUS", -251), ("StartingFlg", 2),
])
def test_invalid_player_values_fail(field, value):
    data = boxscore_data()
    data["HomeBoxscores"][1][field] = value
    with pytest.raises(ValidationError):
        parse(data)


@pytest.mark.parametrize("field", ["PeriodCategory", "PlayerID", "TeamID", "FOULON", "PT2A"])
def test_missing_required_key_is_parse_error(field):
    data = boxscore_data()
    del data["HomeBoxscores"][1][field]
    with pytest.raises(ParseError):
        parse(data)


def test_team_total_rebounds_need_not_equal_player_sum():
    result = parse()
    assert result.teams[0].stats.oreb != sum(p.stats.oreb for p in result.players[:2])


def test_team_shooting_counts_must_equal_player_sum():
    data = boxscore_data()
    data["HomeBoxscores"][-1]["PT2A"] += 1
    with pytest.raises(ValidationError, match="合計"):
        parse(data)


@pytest.mark.parametrize("mutation", ["no_total", "two_totals", "two_players", "no_players"])
def test_invalid_row_cardinality(mutation):
    data = boxscore_data()
    rows = data["HomeBoxscores"]
    if mutation == "no_total":
        rows.pop()
    elif mutation == "two_totals":
        rows.append(copy.deepcopy(rows[-1]))
    elif mutation == "two_players":
        rows.append(copy.deepcopy(rows[1]))
    else:
        data["HomeBoxscores"] = [r for r in rows if not r["PlayerID"]]
    with pytest.raises(ParseError):
        parse(data)


@pytest.mark.parametrize("place,key,value", [
    ("game", "HomeTeamID", "unknown"), ("game", "AwayTeamID", 101),
    ("row", "TeamID", "102"), ("row", "ScheduleKey", "other-game"),
    ("game", "ScheduleKey", "other-game"), ("game", "Event", 5),
    ("game", "GameEndTime", "1700000000"), ("game", "Year", 2016),
])
def test_identity_and_time_conflicts(place, key, value):
    data = boxscore_data()
    target = data["Game"] if place == "game" else data["HomeBoxscores"][1]
    target[key] = value
    with pytest.raises((ParseError, ValidationError)):
        parse(data)


def test_same_player_cannot_appear_on_both_teams():
    data = boxscore_data()
    data["AwayBoxscores"][1]["PlayerID"] = data["HomeBoxscores"][1]["PlayerID"]
    with pytest.raises(ValidationError):
        parse(data)


def test_capacity_even_zero_attendance_is_validated():
    with pytest.raises(ValidationError):
        parse(capacity=500)
    data = boxscore_data()
    data["Game"]["Attendance"] = 0
    assert parse(data, capacity=500).game.attendance == 0
    with pytest.raises(ValidationError):
        parse(data, capacity=0)


@pytest.mark.parametrize("event", [4, 5, 11, 20, True])
def test_excluded_competitions(event):
    with pytest.raises(ValidationError):
        parse_boxscore(page(), event=event, clubs=CLUBS)


def test_playoffs_context_is_retained():
    result = parse_boxscore(page(), event=3, clubs=CLUBS)
    assert result.game.competition == "PLAYOFF"


def test_not_finished_is_distinct_from_bad_html():
    data = boxscore_data()
    data["Game"]["GameEndedFlg"] = False
    with pytest.raises(DataUnavailable):
        parse(data)
    with pytest.raises(DataUnavailable):
        extract_embedded_json('<script>_contexts.scheduleKey = "demo-game";</script>')
    with pytest.raises(ParseError) as error:
        extract_embedded_json('<html>メンテナンス</html>')
    assert not isinstance(error.value, DataUnavailable)


@pytest.mark.parametrize("body", [
    '<script>_contexts_s3id.data = {broken};</script>',
    '<script>_contexts_s3id.data = {"Game": {}, "Game": {}};</script>',
    '<script>_contexts_s3id.data = {"value": NaN};</script>',
    '<script>_contexts_s3id.data = {} + dangerous();</script>',
    '<script>_contexts_s3id.data = {};\n_contexts_s3id.data = {};</script>',
    '<script src="external.js">_contexts_s3id.data = {};</script>',
])
def test_reject_ambiguous_or_non_json_scripts(body):
    with pytest.raises(ParseError):
        extract_embedded_json(body)


def test_inline_json_strings_are_not_evaluated():
    data = boxscore_data()
    data["unused"] = "'); import os; unexpected(); ('"
    assert parse(data).game.id == "demo-game"


@pytest.mark.parametrize("value,expected", [("０", 0), ("1,000", 1000), ("", None),
                                           (None, None), ("-", None), ("DNP", None)])
def test_integer_normalization(value, expected):
    assert integer(value, maximum=2000) == expected


@pytest.mark.parametrize("value", [True, "1,2", "nan", float("nan"), float("inf"), 1.5])
def test_non_integer_data_rejected(value):
    with pytest.raises(ParseError):
        integer(value)


def test_mm_ss_and_epoch_formats():
    assert minutes("０９:０２") == pytest.approx(9 + 2 / 60)
    with pytest.raises(ParseError):
        minutes("10:60")
    a = timestamp("1759331100")
    b = timestamp("/Date(1759331100000+0900)/")
    assert a == b
    assert iso_utc(a) == "2025-10-01T15:05:00Z"


def test_failure_streak_resets_on_success():
    tracker = ParseFailureTracker()
    tracker.failure()
    tracker.failure()
    tracker.success()
    tracker.failure()
    tracker.failure()
    with pytest.raises(ParseErrorStreak):
        tracker.failure()
