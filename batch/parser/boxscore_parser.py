"""終了済み試合の必要項目だけを正規化する。"""

import re
from collections.abc import Mapping
from datetime import timedelta
from zoneinfo import ZoneInfo

from .errors import (
    DataUnavailable,
    OutOfScopeCompetitionError,
    ParseError,
    UnknownClubError,
    ValidationError,
)
from .extract import extract_embedded_json
from .fields import COUNT_FIELDS
from .models import BoxScore, Counts, GameRecord, PlayerStats, TeamStats
from .normalize import flag, integer, iso_utc, minutes, required_integer, source_id, text, timestamp
from .validators import possessions, validate_counts, validate_scoring_total


def required(row: Mapping[str, object], key: str) -> object:
    if key not in row:
        raise ParseError(f"必須フィールドがない: {key}")
    return row[key]


def object_row(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise ParseError("記録がオブジェクトでない")
    return value


def required_text(value: object) -> str:
    result = text(value)
    assert result is not None
    return result


def _counts(row: Mapping[str, object], *, player: bool) -> Counts:
    values = {key: integer(required(row, source), maximum=6 if player and key == "pf" else 250)
              for key, source in COUNT_FIELDS.items()}
    result = Counts(**values)
    validate_counts(result, integer(required(row, "RB_TOT")))
    return result


def _side(
    raw_rows: object, *, game_id: str, team_id: str, club_id: str, is_home: int, score: int,
) -> tuple[list[PlayerStats], TeamStats]:
    if not isinstance(raw_rows, list):
        raise ParseError("ボックススコアが配列でない")
    players: list[PlayerStats] = []
    totals: list[Counts] = []
    #: `Category=2`（選手以外の登録行）。照合の分母に使い、件数もこれで数える
    others: list[Counts] = []
    seen: set[str] = set()
    for raw_row in raw_rows:
        row = object_row(raw_row)
        period = required_integer(required(row, "PeriodCategory"), maximum=100)
        if period != 18:
            continue
        if source_id(required(row, "ScheduleKey")) != game_id:
            raise ValidationError("試合IDが親と一致しない")
        row_team_id = required(row, "TeamID")
        if row_team_id not in (None, "") and source_id(row_team_id) != team_id:
            raise ValidationError("チームIDが親と一致しない")
        player_id = required(row, "PlayerID")
        # **`Category` で分岐する。`PlayerID` の有無で分けない。**
        # 1 = 選手 / 2 = 選手以外の登録行 / 3 = 公式のチーム合計。
        # 2 の意味は年度で違い、2016-17 はヘッドコーチの行（PlayerID を持ち
        # PlayTime は DNP、全スタッツ0）、2025-26 は選手に付かないチーム記録
        # （PlayerID が空で、リバウンド等に値が入る）だった。どちらも
        # 選手ではないため `player_game_stats` には保存しない。
        #
        # **ただし得点を持つ `Category=2` 行が実在し、公式合計にはその得点が入る。**
        # 照合の分母には含める（詳細設計 4.4）。含めないと2016-17 の7試合が落ちた。
        category = required_integer(required(row, "Category"), maximum=3)
        if category == 1:
            if player_id in (None, ""):
                raise ParseError("選手行に PlayerID がない")
            if row_team_id in (None, ""):
                raise ParseError("選手行に所属がない")
            pid = source_id(player_id)
            if pid in seen:
                raise ParseError("選手の通算行が重複")
            seen.add(pid)
            number = required(row, "PlayerNo")
            if isinstance(number, int) and not isinstance(number, bool):
                number = str(number)
            players.append(PlayerStats(
                game_id=game_id, player_id=pid, club_id=club_id,
                name=required_text(required(row, "PlayerNameJ")),
                number=text(number, optional=True),
                started=flag(required(row, "StartingFlg")),
                minutes=minutes(required(row, "PlayTime")),
                plus_minus=integer(row.get("PLUSMINUS"), minimum=-250),
                stats=_counts(row, player=True),
            ))
        elif category == 3:
            if player_id not in (None, ""):
                raise ParseError("公式合計行に PlayerID がある")
            totals.append(_counts(row, player=False))
        else:  # category == 2
            # 公式合計へ加算はしないが、照合の分母には入れる
            others.append(_counts(row, player=False))
    if len(totals) != 1 or not players:
        raise ParseError("公式合計行の欠落または重複")
    total = totals[0]
    if total.pts != score:
        raise ValidationError("ボックススコア合計と試合得点が一致しない")
    validate_scoring_total(total, players, others)
    return players, TeamStats(game_id, club_id, is_home, total, possessions(total))


def parse_boxscore(
    body: str, *, event: int, clubs: Mapping[str, str], expected_game_id: str | None = None,
    capacity: int | None = None,
) -> BoxScore:
    """clubsは公式TeamID→内部club_id。大会区分は日程から渡す。"""
    if isinstance(event, bool) or event not in (2, 3):
        raise ValidationError("取得対象外の大会区分")
    raw = extract_embedded_json(body)
    game = object_row(required(raw, "Game"))
    ended = flag(required(game, "GameEndedFlg"))
    if ended != 1:
        raise DataUnavailable("終了済み試合でない")
    game_id = source_id(required(game, "ScheduleKey"))
    if expected_game_id is not None and game_id != expected_game_id:
        raise ValidationError("要求した試合と応答のIDが一致しない")
    for container in (raw, game):
        if "ScheduleKey" in container and source_id(container["ScheduleKey"]) != game_id:
            raise ValidationError("試合IDの階層間不一致")
        if (container.get("Event") not in (None, "")
                and required_integer(container["Event"]) != event):
            # **データの欠陥ではなく対象外である**（詳細設計 4.4）。オールスター等が
            # `event=2` の日程に混ざる。`不正` に数えず、打ち切り counter にも入れない
            raise OutOfScopeCompetitionError("日程と試合詳細の大会区分が一致しない")
    year = required_integer(required(game, "Year"), minimum=1, maximum=9999)
    tipoff = timestamp(required(game, "GameDateTime"))
    jst_date = tipoff.astimezone(ZoneInfo("Asia/Tokyo")).date()
    if jst_date.year not in (year, year + 1):
        raise ValidationError("試合日と年度が一致しない")
    end_value = game.get("GameEndTime")
    estimated = end_value in (None, "")
    finished = tipoff + timedelta(hours=2) if estimated else timestamp(end_value)
    if finished < tipoff:
        raise ValidationError("終了時刻が開始時刻より前")
    home_id, away_id = (source_id(required(game, f"{side}TeamID")) for side in ("Home", "Away"))
    if home_id not in clubs or away_id not in clubs:
        # **クラブ対応表に無い = リーグ戦ではない**（選抜チーム・海外クラブ・下位
        # リーグ）。データの欠陥ではないので `ValidationError` と区別する
        # （要件 5.3 の絞り込み3段目。詳細設計 4.4）
        raise UnknownClubError("club_source_ids で解決できない TeamID")
    if home_id == away_id:
        raise ValidationError("対戦クラブを解決できない")
    home_club, away_club = clubs[home_id], clubs[away_id]
    if not home_club or not away_club or home_club == away_club:
        raise ValidationError("クラブ対応表が不正")
    home_score = required_integer(required(game, "HomeTeamScore"))
    away_score = required_integer(required(game, "AwayTeamScore"))
    attendance = integer(game.get("Attendance"), maximum=200000)
    if capacity is not None:
        if isinstance(capacity, bool) or capacity <= 0:
            raise ValidationError("収容人数が不正")
        if attendance is not None and attendance > capacity * 1.2:
            raise ValidationError("入場者数が収容人数の許容範囲外")
    venue = game.get("StadiumCD")
    record = GameRecord(
        id=game_id, year=year, competition="REGULAR" if event == 2 else "PLAYOFF",
        game_date=jst_date.isoformat(), tipoff_at=iso_utc(tipoff), finished_at=iso_utc(finished),
        finished_at_is_estimated=int(estimated), home_club_id=home_club, away_club_id=away_club,
        home_name=required_text(required(game, "HomeTeamNameJ")),
        away_name=required_text(required(game, "AwayTeamNameJ")),
        venue_id=source_id(venue) if venue not in (None, "") else None,
        venue_name=text(game.get("StadiumNameJ"), optional=True),
        home_score=home_score, away_score=away_score, attendance=attendance,
    )
    home_players, home_team = _side(
        required(raw, "HomeBoxscores"), game_id=game_id, team_id=home_id,
        club_id=home_club, is_home=1, score=home_score,
    )
    away_players, away_team = _side(
        required(raw, "AwayBoxscores"), game_id=game_id, team_id=away_id,
        club_id=away_club, is_home=0, score=away_score,
    )
    players = home_players + away_players
    if len({p.player_id for p in players}) != len(players):
        raise ValidationError("同一選手が両チームに存在")
    return BoxScore(record, tuple(players), (home_team, away_team))


#: 試合詳細ページの会場ID（`/arena_detail/?ArenaCD=<id>` のリンクと埋め込みの両方）。
_ARENA_CD = re.compile(r'ArenaCD(?:=|"\s*:\s*"?)(\d+)')


def parse_arena_cd(body: str) -> str | None:
    """**試合前**の試合詳細ページから会場の公式IDを取る（2026-10-09 に実測）。

    設計は長らく「公式の `StadiumCD` はボックススコア（試合後）にしかない」と
    書いていたが、**試合前のページを実際に見た記録が無く、推測だった**。
    実測すると `StadiumCD` / `StadiumNameJ` は0回、**`ArenaCD` が1回**出る。
    値は `venues.id`（= `StadiumCD`）と同じ採番である
    （詳細設計 4.10 が `/arena_detail/?ArenaCD=<id>` に `venues.id` を渡している）。

    **これで会場名による名寄せをせずに済む**（詳細設計 1.1 を変えない）。

    | 返り値 | 意味 |
    |---|---|
    | 文字列 | 一意に決まった会場ID |
    | `None` | **ページに無い。** 推測で埋めない（規約5） |
    | `ParseError` | **2つ以上の異なる値がある。** どれか選べない |

    **複数あったら黙って1つ選ばない。** 選ぶと、構造が変わったときに
    別の会場のIDが静かに入る。
    """
    found = {match.group(1) for match in _ARENA_CD.finditer(body)}
    if not found:
        return None
    if len(found) > 1:
        raise ParseError(f"会場IDが一意でない: {len(found)}件")
    return found.pop()
