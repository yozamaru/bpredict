"""Parse season club selectors and schedule JSON without HTTP or database access.

Only the list's game rows are records: nested media links are not games. Dates
are carried explicitly between pages because the source can omit a repeated
date heading at a pagination boundary.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlsplit

from batch.parser.errors import ParseError, ValidationError

_ORIGIN = "https://www.bleague.jp"
_JST = timezone(timedelta(hours=9))
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}\Z")
_DATE_HEADING = re.compile(
    r"([0-9]{4})[./-]([0-9]{1,2})[./-]([0-9]{1,2})(?:\([月火水木金土日]\))?\Z"
)
_CLOCK = re.compile(r"([0-9]{1,2}):([0-9]{2})\Z")
#: チャンピオンシップ等の区画では、行が**日付の span を1つ余分に持つ**。
#: 通常の区画は `[節, 県|会場, 時刻]` の3つで、ステージ区画は
#: `[ステージ名, 県|会場, 05/13 (土), 16:05]` の4つだった（実データで確認）。
#: 見出しがステージ名で日付を持たないため、日付はこの span から取る。
_ROW_DATE = re.compile(r"([0-9]{1,2})/([0-9]{1,2})\s*(?:\([月火水木金土日]\))?\Z")
_VOID_ELEMENTS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
     "source", "track", "wbr"}
)
_STATES = {
    "見どころ": "SCHEDULED",
    "FINAL": "FINISHED",
    "延期": "POSTPONED",
    "試合延期": "POSTPONED",
    "中止": "CANCELLED",
    "試合中止": "CANCELLED",
}


@dataclass(frozen=True)
class ScheduleGame:
    game_id: str
    competition: str
    game_date: str
    tipoff_at: str | None
    home_source_id: str
    away_source_id: str
    home_name: str
    away_name: str
    home_score: int | None
    away_score: int | None
    status: str
    source_url: str


@dataclass(frozen=True)
class SchedulePage:
    games: tuple[ScheduleGame, ...]
    next_index: int | None
    last_date: str | None
    #: 取り込み対象外として飛ばした行数（オールスター・国際試合など）
    skipped: int = 0
    #: 状態がサーバ側に書かれていないため飛ばした行数。**別に数える** —
    #: 非リーグ戦と混ぜると、どちらが起きたのか出力から分からない
    unresolved: int = 0


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str | None] = field(default_factory=dict)
    children: list[_Node | str] = field(default_factory=list)

    def has_class(self, name: str) -> bool:
        return name in (self.attrs.get("class") or "").split()

    def text(self) -> str:
        """表示される文字だけを返す。**`<script>` / `<style>` の中身は文字ではない。**

        除外しないと、行の状態欄に埋め込まれた JS がそのまま状態の文字列になる
        （2018-19 の CS で、`info-scorestate` が `<script>` だけの行が実在した）。
        """
        return "".join(child.text() if isinstance(child, _Node) else child
                       for child in self.children
                       if not (isinstance(child, _Node) and child.tag in ("script", "style")))

    def descendants(self) -> Iterator[_Node]:
        for child in self.children:
            if isinstance(child, _Node):
                yield child
                yield from child.descendants()


class _Document(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("root")
        self.stack = [self.root]
        self.feed(html)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in _VOID_ELEMENTS:
            if len(self.stack) >= 256:
                raise ParseError("schedule HTML nesting limit exceeded")
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_ELEMENTS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        # HTMLParser is not a browser DOM builder. Close a matching ancestor so
        # optional/unmatched layout tags do not swallow subsequent game rows.
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def _text(node: _Node) -> str:
    return " ".join(node.text().split())


def _identifier(value: str | None) -> str:
    if value is None or _IDENTIFIER.fullmatch(value) is None:
        raise ParseError("invalid or missing source identifier")
    return value


def _one(nodes: list[_Node], label: str) -> _Node:
    if len(nodes) != 1:
        raise ParseError(f"missing or ambiguous {label}")
    return nodes[0]


def _with_class(node: _Node, name: str) -> list[_Node]:
    return [child for child in node.descendants() if child.has_class(name)]


def parse_club_options(html: str) -> dict[str, str]:
    """Return the supplied season's short club names mapped to official IDs."""
    document = _Document(html)
    selectors = [node for node in document.root.descendants()
                 if node.tag == "select" and node.attrs.get("name") == "club"]
    if not selectors:
        raise ParseError("missing season club selector")
    clubs: dict[str, str] = {}
    names_by_id: dict[str, str] = {}
    for selector in selectors:
        options = [node for node in selector.descendants() if node.tag == "option"]
        for option in options:
            raw_id = option.attrs.get("value")
            if raw_id == "":  # The unfiltered 'all clubs' option has no ID.
                continue
            source_id = _identifier(raw_id)
            name = _text(option)
            if not name:
                raise ParseError("missing club name")
            if name in clubs and clubs[name] != source_id:
                raise ParseError("conflicting club names in season selector")
            if source_id in names_by_id and names_by_id[source_id] != name:
                raise ParseError("conflicting club identifiers in season selector")
            clubs[name] = source_id
            names_by_id[source_id] = name
    if not clubs:
        raise ParseError("empty season club selector")
    return clubs


def _season_date(value: date, year: int) -> str:
    if value.year not in (year, year + 1):
        raise ValidationError("schedule date is outside requested season years")
    return value.isoformat()


def _previous_date(value: str | None, year: int) -> str | None:
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValidationError("invalid previous schedule date") from None
    if parsed.isoformat() != value:
        raise ValidationError("previous schedule date must use ISO calendar form")
    return _season_date(parsed, year)


def _heading_date(node: _Node, year: int) -> str | None:
    """日付見出しを読む。**日付でない見出しは None を返す。**

    `event=2`（リーグ戦）の中に、日付ではない見出しの区画が混ざる。
    2016-17 の実データでオールスター（`B.BLACK` 対 `B.WHITE`）と国際試合
    （`川崎` 対 `安養KGC`）がこの形で現れた。要件 5.3 は「オールスターは
    `event=5`」としていたが、**古い年度では `event=2` に混在する**。

    区画ごと飛ばすため、ここで例外にしない。ただし**前の日付を引き継がせない**
    （引き継ぐと非リーグ戦に誤った日付が付く）。
    """
    title = _one(_with_class(node, "title"), "schedule date heading")
    value = unicodedata.normalize("NFKC", _text(title)).replace(" ", "")
    matched = _DATE_HEADING.fullmatch(value)
    if matched is None:
        return None
    try:
        parsed = date(*(int(part) for part in matched.groups()))
    except ValueError:
        raise ValidationError("invalid schedule calendar date") from None
    return _season_date(parsed, year)


class _NoScheduleDate(Exception):
    """見出しも行も日付を持たない。取り込み対象外として飛ばす。"""


class _NotALeagueGame(Exception):
    """その年度のクラブ一覧にないチームの試合。取り込み対象外として飛ばす。"""


class _UnresolvedState(Exception):
    """サーバが試合の状態を書いていない行。状態を推測せずに飛ばす。"""


def _season_year(month: int, year: int) -> int:
    """シーズン内の月から暦年を決める。

    シーズンは9〜12月に開幕し翌年5月ごろまで続く。1〜8月は翌年である。
    範囲の妥当性は `_season_date` が併せて検査する。
    """
    return year if month >= 9 else year + 1


def _row_schedule(node: _Node, year: int, heading_date: str | None) -> tuple[str, str | None]:
    """行から `(game_date, tipoff_at)` を決める。

    通常の区画は見出しが日付で、行の時刻は `HH:MM` だけを持つ。
    チャンピオンシップ等の区画は見出しがステージ名で、**行の時刻に日付が付く**
    （例: `05/13 (土)16:05`）。後者では行の日付を使う。
    """
    containers = _with_class(node, "info-arena")
    value = ""
    row_date = ""
    if containers:
        arena = _one(containers, "schedule time container")
        spans = [child for child in arena.children
                 if isinstance(child, _Node) and child.tag == "span"]
        if len(spans) >= 3:
            value = unicodedata.normalize("NFKC", _text(spans[-1]))
        if len(spans) >= 4:
            # ステージ区画では日付が時刻の直前の span に入る
            row_date = unicodedata.normalize("NFKC", _text(spans[-2]))

    dated = _ROW_DATE.fullmatch(row_date) if row_date else None
    if dated is not None:
        month, day = (int(part) for part in dated.groups())
        try:
            parsed = date(_season_year(month, year), month, day)
        except ValueError:
            raise ValidationError("invalid schedule calendar date") from None
        game_date = _season_date(parsed, year)
        matched = _CLOCK.fullmatch(value) if value else None
        if matched is None:
            if value in ("", "-", "--:--", "未定", "調整中"):
                return game_date, None
            raise ParseError("unrecognized schedule tipoff time")
        return game_date, _to_utc(game_date, int(matched[1]), int(matched[2]))

    if heading_date is None:
        # 見出しも行も日付を持たない。推測で埋めない
        raise _NoScheduleDate(value)
    if value in ("", "-", "--:--", "未定", "調整中"):
        return heading_date, None
    matched = _CLOCK.fullmatch(value)
    if matched is None:
        raise ParseError("unrecognized schedule tipoff time")
    return heading_date, _to_utc(heading_date, int(matched[1]), int(matched[2]))


def _to_utc(game_date: str, hour: int, minute: int) -> str:
    try:
        local = datetime.combine(date.fromisoformat(game_date), datetime.min.time(), _JST)
        local = local.replace(hour=hour, minute=minute)
    except ValueError:
        raise ValidationError("invalid schedule tipoff time") from None
    return local.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _score(node: _Node, side: str) -> int | None:
    score = _one(_with_class(node, f"{side}-score"), "schedule score")
    value = unicodedata.normalize("NFKC", _text(score))
    if value in ("", "-"):
        return None
    if re.fullmatch(r"[0-9]+", value) is None:
        raise ParseError("unrecognized schedule score")
    significant = value.lstrip("0") or "0"
    if len(significant) > 3:
        raise ValidationError("schedule score is outside the allowed range")
    points = int(significant)
    if not 0 <= points <= 250:
        raise ValidationError("schedule score is outside the allowed range")
    return points


def _team(node: _Node, side: str, clubs_by_name: Mapping[str, str]) -> tuple[str, str]:
    team = _one([child for child in node.descendants()
                 if child.has_class("team") and child.has_class(side)], "schedule team")
    name = _text(_one(_with_class(team, "team-name"), "schedule club name"))
    if name not in clubs_by_name:
        # クラブ選択肢はその年度のリーグ所属クラブの正本である。ここにない相手は
        # リーグ戦のカードではない（選抜チーム・海外クラブ・下位リーグ）。
        # **改称は選択肢側も当該年度の名称になるため、取りこぼしにはならない。**
        raise _NotALeagueGame(name)
    return name, _identifier(clubs_by_name[name])


def _no_server_rendered_outcome(row: _Node) -> bool:
    """行にサーバ側で書かれた結果が何もないか。

    2018-19 の CS に、状態欄が空で得点欄も空（または存在しない）行が4件あった。
    いずれも2勝0敗で不要になった第3戦で、状態は `<script>` の `ScheduleState` から
    JS が描く。**`"2"` が中止か延期かの対応は公開されていないため、推測で埋めない。**
    行の操作ボタンの文字（`試合中止` / `配信終了`）は放送・導線の状態であって
    試合の状態ではないので、こちらも状態の出典にしない。

    **得点が入っている行は、状態が空でも飛ばさない**（構造変更の疑いとして落とす）。
    ここを緩めると、終了した試合が静かに取り込まれなくなる。
    """
    return all(_text(node) == "" for side in ("home", "away")
               for node in _with_class(row, f"{side}-score"))


def _check_row_link(row: _Node, game_id: str, status: str) -> None:
    """行が試合詳細を指していることを確かめる。

    **中止・延期の行はリンクを持たない。** その場合 `.data-game` は `<a>` ではなく
    `<div class="... btn disabled">` になる（2018-19 の CS で、2勝0敗で不要になった
    クォーターファイナル第3戦3試合がこの形だった。それまでの実装は `<a>` を必須に
    していたため、CS のページごと `ParseError` で落ち、**シーズン全体が取り込めなかった**）。

    リンクがない行を無条件に許すと、行のIDと試合詳細のIDが一致することの確認が
    静かに消える。**中止・延期に限って許す。**
    """
    if row.tag != "a":
        if status not in ("CANCELLED", "POSTPONED"):
            raise ParseError("schedule game row is not a link")
        return
    try:
        url = urlsplit(row.attrs.get("href") or "")
        keys = parse_qs(url.query, keep_blank_values=True).get("ScheduleKey")
    except ValueError:
        raise ParseError("invalid schedule game link") from None
    if (url.scheme not in ("", "https") or url.netloc not in ("", "www.bleague.jp")
            or url.path != "/game_detail/" or keys != [game_id] or url.fragment):
        raise ParseError("schedule row and game link do not agree")


def _parse_game(
    node: _Node,
    heading_date: str | None,
    year: int,
    competition: str,
    clubs_by_name: Mapping[str, str],
) -> ScheduleGame:
    game_id = _identifier(node.attrs.get("id"))
    row = _one(_with_class(node, "data-game"), "schedule game row")
    state = _text(_one(_with_class(row, "info-scorestate"), "schedule game state"))
    if state == "" and _no_server_rendered_outcome(row):
        raise _UnresolvedState(game_id)
    if state not in _STATES:
        raise ParseError("unrecognized or live schedule game state")
    status = _STATES[state]
    _check_row_link(row, game_id, status)
    game_date, tipoff_at = _row_schedule(row, year, heading_date)
    home_name, home_id = _team(row, "home", clubs_by_name)
    away_name, away_id = _team(row, "away", clubs_by_name)
    if home_id == away_id:
        raise ValidationError("schedule teams must be different")
    home_score, away_score = _score(row, "home"), _score(row, "away")
    if status == "FINISHED":
        if home_score is None or away_score is None:
            raise ParseError("finished schedule game is missing a score")
        if home_score == away_score:
            raise ValidationError("finished schedule game cannot be tied")
    elif home_score is not None or away_score is not None:
        raise ValidationError("unplayed schedule game contains scores")
    return ScheduleGame(
        game_id=game_id, competition=competition, game_date=game_date,
        tipoff_at=tipoff_at, home_source_id=home_id, away_source_id=away_id,
        home_name=home_name, away_name=away_name, home_score=home_score, away_score=away_score,
        status=status,
        source_url=f"{_ORIGIN}/game_detail/?{urlencode({'ScheduleKey': game_id, 'tab': 2})}",
    )


def _schedule_nodes(node: _Node) -> Iterator[_Node]:
    """Visit date headings and outer rows in order, excluding nested media rows."""
    for child in node.children:
        if not isinstance(child, _Node):
            continue
        if child.tag == "li" and child.has_class("list-item"):
            if not child.has_class("item-media"):
                yield child
            continue
        if child.has_class("champion-box"):
            yield child
        yield from _schedule_nodes(child)


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ParseError("duplicate schedule JSON key")
        result[key] = value
    return result


def parse_schedule(
    body: str, *, year: int, event: int, clubs_by_name: Mapping[str, str],
    previous_date: str | None = None, index: int = 0,
) -> SchedulePage:
    """Parse a single page; retain `last_date` for the next request.

    `clubs_by_name` は `parse_club_options()` の戻り値（**短縮名 → 公式ID**）。
    `parse_boxscore()` の `clubs`（公式ID → 内部club_id）とは向きが違う。
    同じ名前にしていたため、実サイトでの確認時に取り違えて失敗した。

    An exact duplicate within the page is emitted once. A conflicting duplicate
    rejects the entire page rather than choosing one version silently.
    """
    if type(year) is not int or not 2016 <= year <= 9998:
        raise ValidationError("invalid schedule season year")
    if type(event) is not int or event not in (2, 3):
        raise ValidationError("unsupported schedule competition")
    if type(index) is not int or index < 0:
        raise ValidationError("invalid requested schedule index")
    last_date = _previous_date(previous_date, year)
    try:
        payload = json.loads(body, object_pairs_hook=_unique_json_object)
    except (TypeError, ValueError, RecursionError):
        raise ParseError("invalid schedule JSON") from None
    if not isinstance(payload, dict) or "topics" not in payload or "index" not in payload:
        raise ParseError("missing schedule JSON fields")
    topics, next_index = payload["topics"], payload["index"]
    if not isinstance(topics, list) or any(not isinstance(part, str) for part in topics):
        raise ParseError("schedule topics must be HTML strings")
    if not topics:
        if next_index is not None:
            raise ParseError("empty schedule page has a continuation index")
        return SchedulePage((), None, last_date)
    # **`index` が null なら、試合があっても最終ページである。**
    # 2016-17 のチャンピオンシップは 15試合 / `index=null` の単一ページで、
    # 進行を必須にしていた実装は**CSを1件も取り込めなかった**。
    if next_index is not None and (type(next_index) is not int or next_index <= index):
        raise ParseError("schedule continuation index did not advance")
    document = _Document("".join(topics))
    games: dict[str, ScheduleGame] = {}
    competition = "REGULAR" if event == 2 else "PLAYOFF"
    skipped = 0
    unresolved = 0
    for node in _schedule_nodes(document.root):
        if node.has_class("champion-box"):
            last_date = _heading_date(node, year)
            continue
        try:
            game = _parse_game(node, last_date, year, competition, clubs_by_name)
        except (_NotALeagueGame, _NoScheduleDate):
            # その年度のクラブ一覧にない相手、または日付が決まらない行。
            # **推測で埋めずに飛ばし、件数を返す。**
            skipped += 1
            continue
        except _UnresolvedState:
            unresolved += 1
            continue
        if game.game_id in games and games[game.game_id] != game:
            raise ParseError("conflicting duplicate schedule game")
        games[game.game_id] = game
    if not games and skipped == 0 and unresolved == 0:
        # 行はあるのに1件も取れず、飛ばした覚えもない → 構造が変わった
        raise ParseError("nonempty schedule topics contain no game rows")
    return SchedulePage(tuple(games.values()), next_index, last_date, skipped, unresolved)
