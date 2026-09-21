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


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str | None] = field(default_factory=dict)
    children: list[_Node | str] = field(default_factory=list)

    def has_class(self, name: str) -> bool:
        return name in (self.attrs.get("class") or "").split()

    def text(self) -> str:
        return "".join(child.text() if isinstance(child, _Node) else child
                       for child in self.children)

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


def _heading_date(node: _Node, year: int) -> str:
    title = _one(_with_class(node, "title"), "schedule date heading")
    value = unicodedata.normalize("NFKC", _text(title)).replace(" ", "")
    matched = _DATE_HEADING.fullmatch(value)
    if matched is None:
        raise ParseError("unrecognized schedule date heading")
    try:
        parsed = date(*(int(part) for part in matched.groups()))
    except ValueError:
        raise ValidationError("invalid schedule calendar date") from None
    return _season_date(parsed, year)


def _tipoff(node: _Node, game_date: str) -> str | None:
    containers = _with_class(node, "info-arena")
    if not containers:
        return None
    arena = _one(containers, "schedule time container")
    spans = [child for child in arena.children if isinstance(child, _Node) and child.tag == "span"]
    if len(spans) < 3:
        return None
    value = unicodedata.normalize("NFKC", _text(spans[-1]))
    if value in ("", "-", "--:--", "未定", "調整中"):
        return None
    matched = _CLOCK.fullmatch(value)
    if matched is None:
        raise ParseError("unrecognized schedule tipoff time")
    try:
        local = datetime.combine(date.fromisoformat(game_date), datetime.min.time(), _JST)
        local = local.replace(hour=int(matched[1]), minute=int(matched[2]))
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


def _team(node: _Node, side: str, clubs: Mapping[str, str]) -> tuple[str, str]:
    team = _one([child for child in node.descendants()
                 if child.has_class("team") and child.has_class(side)], "schedule team")
    name = _text(_one(_with_class(team, "team-name"), "schedule club name"))
    if name not in clubs:
        raise ParseError("schedule club is absent from season selector")
    return name, _identifier(clubs[name])


def _parse_game(
    node: _Node, game_date: str, competition: str, clubs: Mapping[str, str]
) -> ScheduleGame:
    game_id = _identifier(node.attrs.get("id"))
    link = _one([child for child in _with_class(node, "data-game") if child.tag == "a"],
                "schedule game link")
    try:
        url = urlsplit(link.attrs.get("href") or "")
        keys = parse_qs(url.query, keep_blank_values=True).get("ScheduleKey")
    except ValueError:
        raise ParseError("invalid schedule game link") from None
    if (url.scheme not in ("", "https") or url.netloc not in ("", "www.bleague.jp")
            or url.path != "/game_detail/" or keys != [game_id] or url.fragment):
        raise ParseError("schedule row and game link do not agree")
    home_name, home_id = _team(link, "home", clubs)
    away_name, away_id = _team(link, "away", clubs)
    if home_id == away_id:
        raise ValidationError("schedule teams must be different")
    home_score, away_score = _score(link, "home"), _score(link, "away")
    state = _text(_one(_with_class(link, "info-scorestate"), "schedule game state"))
    if state not in _STATES:
        raise ParseError("unrecognized or live schedule game state")
    status = _STATES[state]
    if status == "FINISHED":
        if home_score is None or away_score is None:
            raise ParseError("finished schedule game is missing a score")
        if home_score == away_score:
            raise ValidationError("finished schedule game cannot be tied")
    elif home_score is not None or away_score is not None:
        raise ValidationError("unplayed schedule game contains scores")
    return ScheduleGame(
        game_id=game_id, competition=competition, game_date=game_date,
        tipoff_at=_tipoff(link, game_date), home_source_id=home_id, away_source_id=away_id,
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
    body: str, *, year: int, event: int, clubs: Mapping[str, str],
    previous_date: str | None = None, index: int = 0,
) -> SchedulePage:
    """Parse a single page; retain `last_date` for the next request.

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
    if type(next_index) is not int or next_index <= index:
        raise ParseError("schedule continuation index did not advance")
    document = _Document("".join(topics))
    games: dict[str, ScheduleGame] = {}
    competition = "REGULAR" if event == 2 else "PLAYOFF"
    for node in _schedule_nodes(document.root):
        if node.has_class("champion-box"):
            last_date = _heading_date(node, year)
            continue
        if last_date is None:
            raise ParseError("schedule game has no date heading or previous date")
        game = _parse_game(node, last_date, competition, clubs)
        if game.game_id in games and games[game.game_id] != game:
            raise ParseError("conflicting duplicate schedule game")
        games[game.game_id] = game
    if not games:
        raise ParseError("nonempty schedule topics contain no game rows")
    return SchedulePage(tuple(games.values()), next_index, last_date)
