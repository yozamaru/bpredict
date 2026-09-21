"""Polite, policy-gated HTTP access with a shared, persistent request budget.

All clients on one host must use the same state_path. The separate lock file is
held through waiting, reservation, and transport; replacing the JSON state does
not replace the inode being locked. Response bodies never reach the filesystem.
"""

from __future__ import annotations

import fcntl
import gzip
import hashlib
import io
import json
import math
import os
import random
import re
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol, cast
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser

ORIGIN = "https://www.bleague.jp"
ROBOTS_URL = f"{ORIGIN}/robots.txt"
TERMS_URL = f"{ORIGIN}/site/"
MAX_REQUESTS_PER_DAY = 3_000
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 30.0
# A legitimate holder keeps the lock for one request: the 30s transport timeout
# plus at most 4s of pacing. Waiting past that means the holder is wedged, so fail
# closed instead of blocking the next job forever.
LOCK_WAIT_SECONDS = 60.0


class ScraperError(Exception):
    """Safe public errors: never attach URLs, bodies, or transport messages."""


class ConfigurationError(ScraperError):
    """Invalid client configuration or target URL."""


class PolicyError(ScraperError):
    """Policy approval is missing, stale, changed, or forbids the request."""


class StateError(ScraperError):
    """The shared request state cannot safely be used."""


class RequestLimitError(ScraperError):
    """The UTC day's shared request budget is exhausted."""


class ScrapingStopped(ScraperError):
    """A 429/503 response stops retrieval for the rest of the UTC day."""


class TransportError(ScraperError):
    """A request failed, without exposing its original exception."""


class ResponseError(ScraperError):
    """Unsupported HTTP status, encoding, or oversized response."""


class ResponseHeaders(Protocol):
    def get(self, name: str, default: str = "") -> str: ...


class Response(Protocol):
    @property
    def status(self) -> int: ...

    @property
    def headers(self) -> ResponseHeaders: ...

    def read(self, size: int = -1) -> bytes: ...

    def close(self) -> None: ...


type Transport = Callable[[str, Mapping[str, str], float], Response]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


def _transport(url: str, headers: Mapping[str, str], timeout: float) -> Response:
    opener = build_opener(_NoRedirect())
    request = Request(url, headers=dict(headers), method="GET")
    try:
        return cast(Response, opener.open(request, timeout=timeout))
    except HTTPError as response:
        # HTTPError is also a response. Handle status centrally, including 429/503.
        return cast(Response, response)


def _utc_day(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).date().isoformat()


def _validate_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme == "https"
            and parsed.netloc == "www.bleague.jp"
            and not parsed.fragment
            and not any(ord(character) <= 32 or ord(character) == 127 for character in url)
            and "\\" not in url
        )
    except ValueError:
        valid = False
    if not valid:
        raise ConfigurationError("Only the official HTTPS origin is allowed.")


def _validate_user_agent(user_agent: str) -> None:
    parts = user_agent.split()
    contact = re.search(r"https?://[^\s)]+", user_agent)
    valid = (
        len(parts) >= 2
        and re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]*(?:/[A-Za-z0-9._-]+)?", parts[0])
        and contact is not None
        and all(32 <= ord(character) < 127 for character in user_agent)
    )
    if valid and contact is not None:
        try:
            parsed = urlsplit(contact.group())
            valid = bool(parsed.hostname) and not parsed.username and not parsed.password
        except ValueError:
            valid = False
    if not valid:
        raise ConfigurationError("User-Agent requires an identifier and contact URL.")


def _hash(body: str, *, normalize_lines: bool = False) -> str:
    if normalize_lines:
        body = body.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    values: dict[str, object] = {}
    for key, value in pairs:
        if key in values:
            raise ValueError
        values[key] = value
    return values


@dataclass
class _State:
    version: int
    day: str
    requests: int
    last_request_at: float | None
    blocked: bool


class RateLimitedClient:
    def __init__(
        self,
        user_agent: str,
        state_path: Path,
        robots_sha256: str | None = None,
        terms_sha256: str | None = None,
        *,
        transport: Transport = _transport,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = lambda: random.uniform(0.0, 1.0),
    ) -> None:
        _validate_user_agent(user_agent)
        for digest in (robots_sha256, terms_sha256):
            if digest is not None and re.fullmatch(r"[a-fA-F0-9]{64}", digest) is None:
                raise ConfigurationError("Policy hashes must be SHA256 hex digests.")
        self._user_agent = user_agent
        self._state_path = Path(state_path)
        self._lock_path = self._state_path.with_name(self._state_path.name + ".lock")
        self._robots_sha256 = robots_sha256.lower() if robots_sha256 else None
        self._terms_sha256 = terms_sha256.lower() if terms_sha256 else None
        self._transport = transport
        self._clock = clock
        self._sleep = sleep
        self._jitter = jitter
        self._robots: RobotFileParser | None = None
        self._verified_day: str | None = None

    def inspect_policy(self) -> dict[str, str]:
        """Return candidate digests only; never approve access to game data."""
        self._robots = None
        self._verified_day = None
        robots_body = self._request(ROBOTS_URL)
        robots = self._parse_robots(robots_body)
        self._check_robots(robots, TERMS_URL)
        terms_body = self._request(TERMS_URL)
        return {
            "robots_sha256": _hash(robots_body, normalize_lines=True),
            "terms_sha256": _hash(terms_body),
        }

    def verify_policy(self) -> None:
        """Check approved hashes before allowing requests to game data."""
        self._robots = None
        self._verified_day = None
        if self._robots_sha256 is None or self._terms_sha256 is None:
            raise PolicyError("Approved policy hashes are required.")
        robots_body = self._request(ROBOTS_URL)
        if _hash(robots_body, normalize_lines=True) != self._robots_sha256:
            raise PolicyError("The robots policy changed; review is required.")
        robots = self._parse_robots(robots_body)
        self._check_robots(robots, TERMS_URL)
        terms_body = self._request(TERMS_URL)
        if _hash(terms_body) != self._terms_sha256:
            raise PolicyError("The terms changed; review is required.")
        self._robots = robots
        self._verified_day = _utc_day(self._clock())

    def get(self, url: str) -> str:
        """Fetch UTF-8 text after verification, subject to robots rules."""
        _validate_url(url)
        self._check_policy(url, self._clock())
        return self._request(url, require_policy=True)

    @staticmethod
    def _parse_robots(body: str) -> RobotFileParser:
        robots = RobotFileParser(ROBOTS_URL)
        robots.parse(body.splitlines())
        return robots

    def _check_robots(self, robots: RobotFileParser, url: str) -> None:
        if not robots.can_fetch(self._user_agent, url):
            raise PolicyError("The robots policy disallows this request.")

    def _check_policy(self, url: str, now: float) -> None:
        if self._robots is None or self._verified_day != _utc_day(now):
            raise PolicyError("Policy verification is required for this UTC day.")
        self._check_robots(self._robots, url)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            lock_file = self._lock_path.open("a+b")
        except OSError:
            raise StateError("The shared request lock is unavailable.") from None
        with lock_file:
            # Bounded, not blocking: an unkillable or wedged holder must not stall
            # every later job. Real time is used here; the injected clock paces
            # requests, not lock waits.
            deadline = time.monotonic() + LOCK_WAIT_SECONDS
            while True:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise StateError("The shared request lock is held too long.") from None
                    time.sleep(0.05)
                except OSError:
                    raise StateError("The shared request lock is unavailable.") from None
            try:
                yield
            finally:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                except OSError:
                    raise StateError("The shared request lock cannot be released.") from None

    def _load_state(self, now: float) -> _State:
        try:
            raw = self._state_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _State(1, _utc_day(now), 0, None, False)
        except (OSError, UnicodeError):
            raise StateError("The shared request state cannot be read.") from None
        try:
            data = json.loads(raw, object_pairs_hook=_unique_object)
            if not isinstance(data, dict) or set(data) != {
                "version", "day", "requests", "last_request_at", "blocked"
            }:
                raise ValueError
            if type(data["version"]) is not int or data["version"] != 1:
                raise ValueError
            if not isinstance(data["day"], str):
                raise TypeError
            if date.fromisoformat(data["day"]).isoformat() != data["day"]:
                raise ValueError
            if type(data["requests"]) is not int or not 0 <= data["requests"] <= 3_000:
                raise ValueError
            last = data["last_request_at"]
            if last is not None and (
                type(last) not in (int, float) or not math.isfinite(last) or last < 0
            ):
                raise ValueError
            if data["requests"] > 0 and last is None:
                raise ValueError
            if data["requests"] > 0 and _utc_day(last) != data["day"]:
                raise ValueError
            if type(data["blocked"]) is not bool:
                raise ValueError
            state = _State(**data)
            self._roll_day(state, now)
            return state
        except (TypeError, ValueError, OverflowError):
            raise StateError("The shared request state is invalid.") from None

    @staticmethod
    def _roll_day(state: _State, now: float) -> None:
        today = _utc_day(now)
        if state.day > today or (
            state.last_request_at is not None and state.last_request_at > now
        ):
            raise StateError("The clock precedes the shared request state.")
        if state.day < today:
            state.day = today
            state.requests = 0
            state.blocked = False

    def _save_state(self, state: _State) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self._state_path.parent,
                prefix=".scraper-state-", delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(asdict(state), temporary, sort_keys=True, allow_nan=False)
                temporary.write("\n")
                temporary.flush()
                # No fsync. os.replace already gives atomicity and immediate
                # visibility to other processes on this host, which is all this
                # budget needs. fsync only guards against power loss, and it can
                # block uninterruptibly when the disk stalls: a hung CI job that
                # neither SIGKILL nor the job timeout could stop (see ci.yml).
            os.replace(temporary_path, self._state_path)
            temporary_path = None
        except OSError:
            raise StateError("The shared request state cannot be saved.") from None
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    raise StateError("Temporary request state cannot be removed.") from None

    def _request(self, url: str, *, require_policy: bool = False) -> str:
        _validate_url(url)
        with self._locked():
            now = self._clock()
            state = self._load_state(now)
            if state.blocked:
                raise ScrapingStopped("Scraping is stopped for this UTC day.")
            if state.requests >= MAX_REQUESTS_PER_DAY:
                raise RequestLimitError("The daily request limit has been reached.")
            if state.last_request_at is not None:
                jitter = self._jitter()
                if not math.isfinite(jitter) or not 0 <= jitter <= 1:
                    raise ConfigurationError("Request jitter must be between zero and one.")
                next_at = state.last_request_at + 3.0 + jitter
                if now < next_at:
                    self._sleep(next_at - now)
                    now = self._clock()
                    if now < next_at:
                        raise StateError("The clock did not reach the request interval.")
            self._roll_day(state, now)
            if require_policy:
                self._check_policy(url, now)
            # Persist the reservation before I/O, even if transport subsequently fails.
            state.requests += 1
            state.last_request_at = now
            self._save_state(state)
            try:
                response = self._transport(
                    url,
                    {"User-Agent": self._user_agent, "Accept-Encoding": "identity"},
                    REQUEST_TIMEOUT_SECONDS,
                )
                with closing(response):
                    if response.status in (429, 503):
                        # If a response crosses midnight, stop the day on which it arrived.
                        self._roll_day(state, self._clock())
                        state.blocked = True
                        self._save_state(state)
                        raise ScrapingStopped("Scraping is stopped for this UTC day.")
                    if response.status != 200:
                        raise ResponseError("The server returned an unsupported HTTP status.")
                    return self._read_body(response)
            except ScraperError:
                raise
            except Exception:  # noqa: BLE001 - no underlying transport exception may leak to logs.
                raise TransportError("The HTTP request failed.") from None

    @staticmethod
    def _read_body(response: Response) -> str:
        encoding = response.headers.get("Content-Encoding", "").strip().lower()
        if encoding not in ("", "identity", "gzip"):
            raise ResponseError("The response encoding is unsupported.")
        length = response.headers.get("Content-Length", "")
        if length:
            try:
                if int(length) < 0:
                    raise ValueError
                if int(length) > MAX_RESPONSE_BYTES:
                    raise ResponseError("The response exceeds the size limit.")
            except ValueError:
                raise ResponseError("The response length is invalid.") from None
        wire = bytearray()
        while len(wire) <= MAX_RESPONSE_BYTES:
            chunk = response.read(min(65_536, MAX_RESPONSE_BYTES + 1 - len(wire)))
            if not chunk:
                break
            wire.extend(chunk)
        if len(wire) > MAX_RESPONSE_BYTES:
            raise ResponseError("The response exceeds the size limit.")
        if length and len(wire) != int(length):
            raise ResponseError("The response body is incomplete.")
        wire_body = bytes(wire)
        body = wire_body
        if encoding == "gzip":
            try:
                with gzip.GzipFile(fileobj=io.BytesIO(wire_body)) as compressed:
                    body = compressed.read(MAX_RESPONSE_BYTES + 1)
            except (OSError, EOFError):
                raise ResponseError("The compressed response is invalid.") from None
            if len(body) > MAX_RESPONSE_BYTES:
                raise ResponseError("The decoded response exceeds the size limit.")
        try:
            return body.decode("utf-8")
        except UnicodeError:
            raise ResponseError("The response is not valid UTF-8.") from None
