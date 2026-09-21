"""カナリアの実行境界を検査する。HTTP通信・DBへの接続は行わない。"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from batch.jobs import parser_canary
from batch.parser.errors import DataUnavailable, ParseError, ValidationError
from batch.scraper.client import ScraperError


@pytest.fixture
def canary(monkeypatch, tmp_path):
    monkeypatch.setenv("SCRAPER_USER_AGENT", "CanaryTest/1.0 (+https://example.com/contact)")
    monkeypatch.setenv("SCRAPER_ROBOTS_SHA256", "a" * 64)
    monkeypatch.setenv("SCRAPER_TERMS_SHA256", "b" * 64)
    monkeypatch.setenv("SCRAPER_STATE_PATH", str(tmp_path / "state.json"))
    client = Mock()
    client.get.return_value = "synthetic response"
    client.inspect_policy.return_value = {"robots_sha256": "c" * 64, "terms_sha256": "d" * 64}
    factory = Mock(return_value=client)
    parser = Mock(return_value=SimpleNamespace(teams=(object(), object()), players=(object(),)))
    monkeypatch.setattr(parser_canary, "RateLimitedClient", factory)
    monkeypatch.setattr(parser_canary, "parse_boxscore", parser)
    return SimpleNamespace(client=client, factory=factory, parser=parser, tmp_path=tmp_path)


@pytest.mark.parametrize("name", ["SCRAPER_ROBOTS_SHA256", "SCRAPER_TERMS_SHA256"])
@pytest.mark.parametrize("value", [None, "", " ", "not-a-hash", "f" * 63])
def test_missing_or_invalid_baseline_fails_without_network(canary, monkeypatch, capsys, name, value):
    if value is None:
        monkeypatch.delenv(name)
    else:
        monkeypatch.setenv(name, value)
    assert parser_canary.main([]) == 1
    canary.factory.assert_not_called()
    canary.parser.assert_not_called()
    output = capsys.readouterr()
    assert output.out == ""
    assert "CanaryConfigurationError" in output.err


def test_canary_verifies_before_fetch_and_uses_existing_master(canary, capsys):
    assert parser_canary.main([]) == 0
    assert [call[0] for call in canary.client.method_calls] == ["verify_policy", "get"]
    fetched_url = canary.client.get.call_args.args[0]
    assert "505497" in fetched_url
    canary.parser.assert_called_once()
    assert canary.parser.call_args.args == ("synthetic response",)
    kwargs = canary.parser.call_args.kwargs
    assert kwargs["expected_game_id"] == "505497"
    assert kwargs["event"] == 2
    assert kwargs["clubs"] == {
        row.source_id: row.club_id for row in parser_canary.load_club_source_ids()
    }
    assert len(kwargs["clubs"]) == 30
    assert canary.factory.call_args.kwargs == {
        "user_agent": "CanaryTest/1.0 (+https://example.com/contact)",
        "state_path": canary.tmp_path / "state.json",
        "robots_sha256": "a" * 64,
        "terms_sha256": "b" * 64,
    }
    output = capsys.readouterr()
    assert "teams=2, players=1" in output.out
    assert "505497" not in output.out
    assert "synthetic response" not in output.out
    assert output.err == ""


def test_inspection_prints_only_candidates_and_never_trusts_them(canary, monkeypatch, capsys):
    monkeypatch.delenv("SCRAPER_ROBOTS_SHA256")
    monkeypatch.delenv("SCRAPER_TERMS_SHA256")
    assert parser_canary.main(["--inspect-policy"]) == 0
    assert [call[0] for call in canary.client.method_calls] == ["inspect_policy"]
    canary.parser.assert_not_called()
    assert canary.factory.call_args.kwargs["robots_sha256"] is None
    assert canary.factory.call_args.kwargs["terms_sha256"] is None
    output = capsys.readouterr()
    assert json.loads(output.out) == {"robots_sha256": "c" * 64, "terms_sha256": "d" * 64}
    assert output.err == ""
    assert parser_canary.main([]) == 1  # 候補を取得しても次の通常実行は承認済みにならない。
    canary.client.verify_policy.assert_not_called()
    canary.client.get.assert_not_called()


def test_custom_game_event_and_default_state_path(canary, monkeypatch):
    monkeypatch.delenv("SCRAPER_STATE_PATH")
    assert parser_canary.main(["--game-id", "123456", "--event", "3"]) == 0
    assert canary.parser.call_args.kwargs["event"] == 3
    assert canary.parser.call_args.kwargs["expected_game_id"] == "123456"
    assert canary.factory.call_args.kwargs["state_path"] == Path("batch/.scraper-state/state.json")


def test_invalid_game_id_fails_before_policy_network(canary):
    assert parser_canary.main(["--game-id", "../private"]) == 1
    canary.client.verify_policy.assert_not_called()
    canary.client.get.assert_not_called()
    canary.parser.assert_not_called()


def test_policy_failure_never_fetches_game(canary, capsys):
    canary.client.verify_policy.side_effect = RuntimeError("https://example.com/SECRET-ID")
    assert parser_canary.main([]) == 1
    canary.client.get.assert_not_called()
    canary.parser.assert_not_called()
    output = capsys.readouterr()
    assert "RuntimeError" in output.err
    assert "SECRET-ID" not in output.err
    assert "https://" not in output.err


@pytest.mark.parametrize("stage", ["get", "parser"])
@pytest.mark.parametrize("error", [ParseError, ValidationError, DataUnavailable, ScraperError, OSError])
def test_failures_return_one_without_raw_exception(canary, capsys, stage, error):
    target = canary.parser if stage == "parser" else canary.client.get
    target.side_effect = error("https://example.com/SECRET-ID <html>private body</html>")
    assert parser_canary.main([]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert error.__name__ in output.err
    for forbidden in ("https://", "SECRET-ID", "private body", "Traceback"):
        assert forbidden not in output.err


def test_inspection_failure_is_sanitized(canary, capsys):
    canary.client.inspect_policy.side_effect = OSError("raw policy body")
    assert parser_canary.main(["--inspect-policy"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "OSError" in output.err
    assert "raw policy body" not in output.err
