"""Synthetic HTTP responses; no live requests and no stored source HTML."""

from __future__ import annotations

import fcntl
import gzip
import hashlib
import io
import json
import threading
import traceback
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

from batch.scraper.boxscore import boxscore_url
from batch.scraper.client import (
    MAX_RESPONSE_BYTES,
    ROBOTS_URL,
    TERMS_URL,
    ConfigurationError,
    PolicyError,
    RateLimitedClient,
    RequestLimitError,
    Response,
    ResponseError,
    ScrapingStopped,
    StateError,
    TransportError,
    _NoRedirect,
    _transport,
)
from batch.scraper.schedule import schedule_html_url, schedule_url

USER_AGENT = "BPredictTest/1.0 (+https://example.test/contact)"
ROBOTS = "User-agent: *\nDisallow: /private/\n"
TERMS = "<html><body>合成の利用規約</body></html>"
GAME_URL = boxscore_url("100001")


def digest(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class Clock:
    def __init__(self, timestamp: float | None = None):
        self.now = timestamp or datetime(2026, 9, 21, 0, 0, tzinfo=UTC).timestamp()
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class Headers:
    def __init__(self, values: Mapping[str, str]):
        self.values = {key.lower(): value for key, value in values.items()}

    def get(self, name: str, default: str = "") -> str:
        return self.values.get(name.lower(), default)


class FakeResponse:
    def __init__(
        self, body: str | bytes = "", status: int = 200, headers: Mapping[str, str] | None = None
    ):
        self.status = status
        self.headers = Headers(headers or {})
        self.body = io.BytesIO(body.encode("utf-8") if isinstance(body, str) else body)
        self.reads: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.reads.append(size)
        return self.body.read(size)

    def close(self) -> None:
        self.body.close()


class FakeTransport:
    def __init__(self, clock: Clock, responses: list[FakeResponse | Exception]):
        self.clock = clock
        self.responses = responses
        self.calls: list[tuple[str, dict[str, str], float, float]] = []

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float) -> Response:
        self.calls.append((url, dict(headers), timeout, self.clock()))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def make_client(
    state_path: Path,
    clock: Clock,
    transport: FakeTransport,
    robots: str | None = digest(ROBOTS),
    terms: str | None = digest(TERMS),
) -> RateLimitedClient:
    return RateLimitedClient(
        USER_AGENT, state_path, robots, terms,
        transport=transport, clock=clock, sleep=clock.sleep, jitter=lambda: 0.25,
    )


def policy_responses() -> list[FakeResponse | Exception]:
    return [FakeResponse(ROBOTS), FakeResponse(TERMS)]


def read_state(state_path: Path) -> dict[str, object]:
    return cast(dict[str, object], json.loads(state_path.read_text()))


def write_state(state_path: Path, clock: Clock, **changes) -> None:
    state = {
        "version": 1, "day": datetime.fromtimestamp(clock(), UTC).date().isoformat(),
        "requests": 1, "last_request_at": clock(), "blocked": False,
    }
    state.update(changes)
    state_path.write_text(json.dumps(state))


def test_policy_verification_rate_headers_timeout_and_no_raw_storage(tmp_path):
    clock = Clock()
    state_path = tmp_path / "shared" / "state.json"
    transport = FakeTransport(clock, policy_responses() + [FakeResponse("synthetic response")])
    client = make_client(state_path, clock, transport)
    client.verify_policy()
    assert client.get(GAME_URL) == "synthetic response"
    assert [call[0] for call in transport.calls] == [ROBOTS_URL, TERMS_URL, GAME_URL]
    assert [call[3] - transport.calls[0][3] for call in transport.calls] == [0, 3.25, 6.5]
    assert all(call[1]["User-Agent"] == USER_AGENT for call in transport.calls)
    assert all(call[1]["Accept-Encoding"] == "identity" for call in transport.calls)
    assert all(call[2] == 30 for call in transport.calls)
    state = read_state(state_path)
    assert state["requests"] == 3
    assert state["blocked"] is False
    assert set(state_path.parent.iterdir()) == {state_path, state_path.with_suffix(".json.lock")}
    assert "synthetic response" not in state_path.read_text()
    assert "利用規約" not in state_path.read_text()


def test_inspection_returns_hashes_and_does_not_approve(tmp_path):
    clock = Clock()
    transport = FakeTransport(clock, policy_responses())
    client = make_client(tmp_path / "state.json", clock, transport, robots=None, terms=None)
    assert client.inspect_policy() == {"robots_sha256": digest(ROBOTS), "terms_sha256": digest(TERMS)}
    with pytest.raises(PolicyError):
        client.get(GAME_URL)
    with pytest.raises(PolicyError):
        client.verify_policy()
    assert len(transport.calls) == 2


@pytest.mark.parametrize("robots,terms", [(None, None), (None, digest(TERMS)), (digest(ROBOTS), None)])
def test_missing_policy_hash_never_requests(tmp_path, robots, terms):
    clock = Clock()
    transport = FakeTransport(clock, [])
    client = make_client(tmp_path / "state.json", clock, transport, robots, terms)
    with pytest.raises(PolicyError):
        client.verify_policy()
    with pytest.raises(PolicyError):
        client.get(GAME_URL)
    assert transport.calls == []


def test_robots_line_endings_normalized_but_full_terms_are_exact(tmp_path):
    clock = Clock()
    transport = FakeTransport(clock, [FakeResponse(ROBOTS.replace("\n", "\r\n")), FakeResponse(TERMS)])
    make_client(tmp_path / "state.json", clock, transport).verify_policy()
    changed = FakeTransport(clock, [FakeResponse(ROBOTS), FakeResponse(TERMS + "\n")])
    client = make_client(tmp_path / "state.json", clock, changed)
    with pytest.raises(PolicyError, match="terms changed"):
        client.verify_policy()
    with pytest.raises(PolicyError):
        client.get(GAME_URL)
    assert len(changed.calls) == 2


def test_changed_robots_stops_before_terms_or_game_request(tmp_path):
    clock = Clock()
    transport = FakeTransport(clock, [FakeResponse(ROBOTS + "# changed\n")])
    client = make_client(tmp_path / "state.json", clock, transport)
    with pytest.raises(PolicyError, match="robots policy changed"):
        client.verify_policy()
    assert len(transport.calls) == 1


def test_robots_disallow_is_enforced_after_verification(tmp_path):
    clock = Clock()
    transport = FakeTransport(clock, policy_responses())
    client = make_client(tmp_path / "state.json", clock, transport)
    client.verify_policy()
    with pytest.raises(PolicyError, match="disallows"):
        client.get("https://www.bleague.jp/private/data")
    assert len(transport.calls) == 2


def test_disallowed_terms_are_not_fetched(tmp_path):
    clock = Clock()
    robots = "User-agent: *\nDisallow: /site/\n"
    transport = FakeTransport(clock, [FakeResponse(robots)])
    client = make_client(tmp_path / "state.json", clock, transport, robots=digest(robots))
    with pytest.raises(PolicyError, match="disallows"):
        client.verify_policy()
    assert len(transport.calls) == 1


@pytest.mark.parametrize("url", [
    "http://www.bleague.jp/game_detail/", "https://bleague.jp/", "https://evil.test/",
    "https://www.bleague.jp.evil.test/", "https://user:secret@www.bleague.jp/",
    "https://www.bleague.jp:444/", "https://www.bleague.jp/#fragment",
    "https://www.bleague.jp/\nsecret", "https://www.bleague.jp\\@evil.test/",
])
def test_invalid_target_rejected_without_network(tmp_path, url):
    clock = Clock()
    transport = FakeTransport(clock, [])
    client = make_client(tmp_path / "state.json", clock, transport)
    with pytest.raises(ConfigurationError):
        client.get(url)
    assert transport.calls == []


@pytest.mark.parametrize("user_agent", ["", "BPredict", "https://example.test", "B\n https://example.test", "B (+https://)"])
def test_invalid_user_agent_rejected(tmp_path, user_agent):
    with pytest.raises(ConfigurationError):
        RateLimitedClient(user_agent, tmp_path / "state.json")


def test_malformed_hash_rejected(tmp_path):
    with pytest.raises(ConfigurationError):
        RateLimitedClient(USER_AGENT, tmp_path / "state.json", robots_sha256="secret")


def test_request_count_and_intervals_survive_client_restart(tmp_path):
    clock = Clock()
    state_path = tmp_path / "state.json"
    first = FakeTransport(clock, policy_responses())
    make_client(state_path, clock, first).verify_policy()
    second = FakeTransport(clock, policy_responses() + [FakeResponse("new")])
    client = make_client(state_path, clock, second)
    client.verify_policy()
    assert client.get(GAME_URL) == "new"
    assert read_state(state_path)["requests"] == 5
    assert second.calls[0][3] - first.calls[-1][3] == 3.25


def test_policy_requests_consume_last_daily_slots(tmp_path):
    clock = Clock()
    state_path = tmp_path / "state.json"
    write_state(state_path, clock, requests=2_998)
    transport = FakeTransport(clock, policy_responses())
    client = make_client(state_path, clock, transport)
    client.verify_policy()
    with pytest.raises(RequestLimitError):
        client.get(GAME_URL)
    assert len(transport.calls) == 2
    assert read_state(state_path)["requests"] == 3_000


def test_failed_attempt_reserved_before_transport_and_not_retried(tmp_path):
    clock = Clock()
    state_path = tmp_path / "state.json"
    transport = FakeTransport(clock, policy_responses() + [OSError("secret url and body")])
    client = make_client(state_path, clock, transport)
    client.verify_policy()
    original_transport = transport.__call__

    def check_reservation(url, headers, timeout):
        assert read_state(state_path)["requests"] == 3
        return original_transport(url, headers, timeout)

    client._transport = check_reservation
    with pytest.raises(TransportError) as error:
        client.get(GAME_URL)
    assert "secret" not in "".join(traceback.format_exception(error.value))
    assert read_state(state_path)["requests"] == 3
    assert len(transport.calls) == 3


@pytest.mark.parametrize("status", [429, 503])
def test_stop_is_persistent_across_clients_until_next_utc_day(tmp_path, status):
    clock = Clock()
    state_path = tmp_path / "state.json"
    transport = FakeTransport(clock, policy_responses() + [FakeResponse("", status=status)])
    client = make_client(state_path, clock, transport)
    client.verify_policy()
    with pytest.raises(ScrapingStopped):
        client.get(GAME_URL)
    assert read_state(state_path)["blocked"] is True
    restarted_transport = FakeTransport(clock, policy_responses() + [FakeResponse("ok")])
    restarted = make_client(state_path, clock, restarted_transport)
    with pytest.raises(ScrapingStopped):
        restarted.verify_policy()
    assert restarted_transport.calls == []
    clock.now += 86_400
    restarted.verify_policy()
    assert restarted.get(GAME_URL) == "ok"
    assert read_state(state_path)["requests"] == 3
    assert read_state(state_path)["blocked"] is False


def test_429_during_policy_also_stops_other_clients(tmp_path):
    clock = Clock()
    state_path = tmp_path / "state.json"
    transport = FakeTransport(clock, [FakeResponse(status=429)])
    with pytest.raises(ScrapingStopped):
        make_client(state_path, clock, transport).inspect_policy()
    with pytest.raises(ScrapingStopped):
        make_client(state_path, clock, transport).verify_policy()
    assert len(transport.calls) == 1


def test_new_utc_day_requires_new_policy_verification(tmp_path):
    clock = Clock()
    transport = FakeTransport(clock, policy_responses())
    client = make_client(tmp_path / "state.json", clock, transport)
    client.verify_policy()
    clock.now += 86_400
    with pytest.raises(PolicyError):
        client.get(GAME_URL)
    assert len(transport.calls) == 2


def test_wait_crossing_utc_midnight_resets_count_but_preserves_interval(tmp_path):
    clock = Clock(datetime(2026, 9, 21, 23, 59, 59, tzinfo=UTC).timestamp())
    state_path = tmp_path / "state.json"
    write_state(state_path, clock, requests=2_999, last_request_at=clock())
    transport = FakeTransport(clock, policy_responses())
    make_client(state_path, clock, transport).verify_policy()
    assert read_state(state_path)["day"] == "2026-09-22"
    assert read_state(state_path)["requests"] == 2
    assert clock.sleeps == [3.25, 3.25]


@pytest.mark.parametrize("state", ["", "{", "null", "[]", '{"secret": "do not log"}'])
def test_corrupt_state_fails_closed(tmp_path, state):
    clock = Clock()
    state_path = tmp_path / "state.json"
    state_path.write_text(state)
    transport = FakeTransport(clock, [])
    with pytest.raises(StateError) as error:
        make_client(state_path, clock, transport).inspect_policy()
    assert state_path.read_text() == state
    assert "secret" not in str(error.value)
    assert transport.calls == []


@pytest.mark.parametrize("changes", [
    {"requests": True}, {"requests": -1}, {"requests": 3_001}, {"version": True},
    {"last_request_at": None}, {"last_request_at": float("nan")},
    {"blocked": 1}, {"day": "2026-09-22"}, {"day": "20260921"},
])
def test_invalid_state_fields_fail_closed(tmp_path, changes):
    clock = Clock()
    state_path = tmp_path / "state.json"
    write_state(state_path, clock, **changes)
    transport = FakeTransport(clock, [])
    with pytest.raises(StateError):
        make_client(state_path, clock, transport).inspect_policy()
    assert transport.calls == []


@pytest.mark.parametrize("body,headers", [
    (b"x" * (MAX_RESPONSE_BYTES + 1), {}),
    (gzip.compress(b"x" * (MAX_RESPONSE_BYTES + 1)), {"Content-Encoding": "gzip"}),
    (b"invalid", {"Content-Encoding": "gzip"}),
    (b"\xff", {}),
    (b"invalid", {"Content-Encoding": "br"}),
    (b"small", {"Content-Length": str(MAX_RESPONSE_BYTES + 1)}),
    (b"small", {"Content-Length": "invalid"}),
])
def test_response_limits_and_invalid_encodings(tmp_path, body, headers):
    clock = Clock()
    response = FakeResponse(body, headers=headers)
    transport = FakeTransport(clock, policy_responses() + [response])
    client = make_client(tmp_path / "state.json", clock, transport)
    client.verify_policy()
    with pytest.raises(ResponseError):
        client.get(GAME_URL)
    assert response.body.closed
    assert all(size <= MAX_RESPONSE_BYTES + 1 for size in response.reads)
    assert len(transport.calls) == 3


def test_gzip_and_exact_decoded_size_limit_are_supported(tmp_path):
    clock = Clock()
    body = "x" * MAX_RESPONSE_BYTES
    response = FakeResponse(gzip.compress(body.encode()), headers={"content-encoding": "gzip"})
    transport = FakeTransport(clock, policy_responses() + [response])
    client = make_client(tmp_path / "state.json", clock, transport)
    client.verify_policy()
    assert client.get(GAME_URL) == body


@pytest.mark.parametrize("status", [301, 302, 307, 308, 404, 500])
def test_http_failure_is_not_retried_or_redirected(tmp_path, status):
    clock = Clock()
    response = FakeResponse(status=status, headers={"Location": "https://evil.test/secret"})
    transport = FakeTransport(clock, policy_responses() + [response])
    client = make_client(tmp_path / "state.json", clock, transport)
    client.verify_policy()
    with pytest.raises(ResponseError):
        client.get(GAME_URL)
    assert len(transport.calls) == 3
    assert response.body.closed


def test_default_transport_retains_http_status_and_disables_redirects(monkeypatch):
    response = HTTPError(GAME_URL, 503, "private error", {}, io.BytesIO())
    opener = Mock()
    opener.open.side_effect = response
    factory = Mock(return_value=opener)
    monkeypatch.setattr("batch.scraper.client.build_opener", factory)
    assert _transport(GAME_URL, {"User-Agent": USER_AGENT}, 30).status == 503
    handler = factory.call_args.args[0]
    assert isinstance(handler, _NoRedirect)
    assert handler.redirect_request(Mock(), Mock(), 302, "redirect", {}, "https://evil.test") is None
    opener.open.assert_called_once()


def test_separate_instances_never_overlap_transport_or_lose_reservations(tmp_path):
    clock = Clock()
    state_path = tmp_path / "state.json"
    entered = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    second_entered = threading.Event()
    calls: list[str] = []

    def first_transport(url, headers, timeout):
        calls.append("first")
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
        return FakeResponse(ROBOTS if url == ROBOTS_URL else TERMS)

    def second_transport(url, headers, timeout):
        second_entered.set()
        calls.append("second")
        return FakeResponse(ROBOTS if url == ROBOTS_URL else TERMS)

    first = RateLimitedClient(
        USER_AGENT, state_path, transport=first_transport,
        clock=clock, sleep=clock.sleep, jitter=lambda: 0,
    )
    second = RateLimitedClient(
        USER_AGENT, state_path, transport=second_transport,
        clock=clock, sleep=clock.sleep, jitter=lambda: 0,
    )

    def second_request():
        second_started.set()
        return second.inspect_policy()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(first.inspect_policy)
        assert entered.wait(5)
        second_future = executor.submit(second_request)
        assert second_started.wait(5)
        assert not second_entered.wait(0.05)
        release.set()
        assert first_future.result(timeout=5)["robots_sha256"] == digest(ROBOTS)
        assert second_future.result(timeout=5)["robots_sha256"] == digest(ROBOTS)
    assert read_state(state_path)["requests"] == 4
    assert len(calls) == 4


def test_lock_wait_is_bounded(tmp_path, monkeypatch):
    """ロック待ちを無制限にしない。

    保持しているプロセスが停止（シグナルで殺せない状態を含む）していると、
    無制限の flock では次のジョブが無言で待ち続ける。CI で実際に起きた。
    上限を超えたら StateError で失敗する（fail closed）。
    """
    monkeypatch.setattr("batch.scraper.client.LOCK_WAIT_SECONDS", 0.0)
    state_path = tmp_path / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    holder = state_path.with_name(state_path.name + ".lock").open("a+b")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
    clock = Clock()
    transport = FakeTransport(clock, policy_responses())
    client = make_client(state_path, clock, transport)
    try:
        with pytest.raises(StateError):
            client.verify_policy()
    finally:
        holder.close()
    assert transport.calls == []


def test_state_is_replaced_atomically_without_fsync(tmp_path, monkeypatch):
    """os.replace の原子性だけで足りる。fsync は呼ばない。

    fsync はディスクが詰まるとシグナルでも中断できない待ちに入る。守るのは
    電源喪失に対する耐久性だけで、この予算には要らない（詳細設計 4.3）。
    """
    calls: list[int] = []
    monkeypatch.setattr("batch.scraper.client.os.fsync", lambda fd: calls.append(fd))
    clock = Clock()
    state_path = tmp_path / "state.json"
    client = make_client(state_path, clock, FakeTransport(clock, policy_responses()))
    client.verify_policy()
    assert read_state(state_path)["requests"] == 2
    assert calls == []


def test_url_helpers_keep_exact_official_parameters():
    assert boxscore_url("123456") == "https://www.bleague.jp/game_detail/?ScheduleKey=123456&tab=2"
    assert schedule_html_url(2025) == "https://www.bleague.jp/schedule/?year=2025&mon=all&tab=1"
    parameters = parse_qs(urlsplit(schedule_url(2025, 3, 20, month=5)).query)
    assert parameters == {
        "data_format": ["json"], "year": ["2025"], "mon": ["5"],
        "tab": ["1"], "event": ["3"], "index": ["20"],
    }


@pytest.mark.parametrize("event", [1, 4, 5, 11, 20, True])
def test_schedule_helpers_reject_unsupported_competitions(event):
    with pytest.raises(ConfigurationError):
        schedule_url(2025, event, 0)


@pytest.mark.parametrize("game_id", ["", "0", "-1", "1&secret=value", "１２", True])
def test_boxscore_helper_rejects_invalid_id(game_id):
    with pytest.raises(ConfigurationError):
        boxscore_url(game_id)
