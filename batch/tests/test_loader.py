"""内部APIクライアントと行数上限の検証（詳細設計 3.4 / 4.8、基本設計 4.3）。"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from batch.loader.api import (
    MAX_ATTEMPTS,
    InternalApi,
    LoaderError,
    RejectedError,
    Response,
)
from batch.loader.limits import chunks, column_count, max_rows_per_request, rows_per_statement

REPO_ROOT = Path(__file__).resolve().parents[2]
TOKEN = "test-token-value"


class Transport:
    """応答を並べて注入する。実際の通信は行わない。"""

    def __init__(self, responses: list[Response]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, bytes | None, dict[str, str]]] = []

    def __call__(self, url, method, body, headers):
        self.calls.append((url, method, body, dict(headers)))
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]


def api(
    responses: list[Response], *, dry_run: bool = False
) -> tuple[InternalApi, Transport]:
    transport = Transport(responses)
    client = InternalApi(
        "http://127.0.0.1:8787",
        TOKEN,
        transport=transport,
        sleep=lambda _: None,
        dry_run=dry_run,
    )
    return client, transport


def ok_body(data: object = None) -> Response:
    return Response(200, json.dumps({"data": data, "meta": {}}))


def test_post_sends_bearer_and_json() -> None:
    client, transport = api([ok_body({"applied": {"games": 1}})])
    result = client.post("games", {"games": [{"id": "g1"}]})

    assert result == {"applied": {"games": 1}}
    url, method, body, headers = transport.calls[0]
    assert url == "http://127.0.0.1:8787/api/v1/internal/games"
    assert method == "POST"
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["Content-Type"] == "application/json"
    assert body is not None
    assert json.loads(body) == {"games": [{"id": "g1"}]}


def test_retries_transient_failures_then_succeeds() -> None:
    """D1 書き込み失敗は3回まで再試行する（基本設計 4.3）。"""
    client, transport = api([Response(503, "{}"), Response(503, "{}"), ok_body({"applied": {}})])
    assert client.post("games", {"games": []}) == {"applied": {}}
    assert len(transport.calls) == MAX_ATTEMPTS


def test_gives_up_after_the_attempt_limit() -> None:
    client, transport = api([Response(500, "{}")])
    with pytest.raises(LoaderError):
        client.post("games", {"games": []})
    assert len(transport.calls) == MAX_ATTEMPTS


@pytest.mark.parametrize("status", [400, 401, 409, 422])
def test_client_errors_are_not_retried(status: int) -> None:
    """4xx は投げ直しても同じ。入力か認可の問題なので止める。"""
    client, transport = api([Response(status, '{"error":{"code":"BAD_REQUEST"}}')])
    with pytest.raises(RejectedError):
        client.post("games", {"games": []})
    assert len(transport.calls) == 1


def test_errors_never_leak_token_or_url() -> None:
    """例外にトークン・URL・応答本文を含めない（絶対ルール4）。

    public リポジトリの Actions ログは全世界から読める。
    """
    client, _ = api([Response(400, json.dumps({"secret": TOKEN, "url": "https://x/y?z=1"}))])
    with pytest.raises(RejectedError) as error:
        client.post("games", {"games": []})
    message = str(error.value)
    assert TOKEN not in message
    assert "127.0.0.1" not in message
    assert "secret" not in message


def test_dry_run_does_not_send() -> None:
    """`--dry-run` は本番D1に書かない（ローカル開発の手順。詳細設計 8.2）。"""
    client, transport = api([ok_body()], dry_run=True)
    client.post("games", {"games": [{"id": "a"}, {"id": "b"}], "teamGames": [{"x": 1}]})
    assert transport.calls == []
    assert client.skipped == [("games", 3)]


def test_ingested_game_ids_parses_the_response() -> None:
    client, transport = api([ok_body({"seasonId": "2016-17-B1", "count": 2, "gameIds": [1, "2"]})])
    assert client.ingested_game_ids("2016-17-B1") == {"1", "2"}
    url, method, _, _ = transport.calls[0]
    assert method == "GET"
    assert url.endswith("/internal/games/ingested?seasonId=2016-17-B1")


@pytest.mark.parametrize("body", ["not json", "[]", '{"data": 1}'])
def test_malformed_responses_fail_closed(body: str) -> None:
    client, _ = api([Response(200, body)])
    if body == '{"data": 1}':
        assert client.get("games/ingested") == 1  # data はそのまま返す
    else:
        with pytest.raises(LoaderError):
            client.get("games/ingested")


@pytest.mark.parametrize(
    ("base", "token"),
    [("ftp://x", TOKEN), ("127.0.0.1:8787", TOKEN), ("http://x", "")],
)
def test_invalid_configuration_is_rejected(base: str, token: str) -> None:
    with pytest.raises(LoaderError):
        InternalApi(base, token)


# --- 行数上限（詳細設計 3.4） ---

def test_limits_follow_the_documented_formula() -> None:
    """`floor(100 / 列数) × 40`。列数は DDL の全列数で数える。"""
    for table in ("games", "player_predictions", "team_games", "venue_source_keys"):
        assert rows_per_statement(table) == 100 // column_count(table)
        assert max_rows_per_request(table) == rows_per_statement(table) * 40


@pytest.mark.parametrize(
    ("table", "columns", "limit"),
    [
        ("player_predictions", 31, 120),
        ("player_game_stats", 24, 160),
        ("games", 25, 160),   # 0009 で venue_name_at_game を追加（上限は 160 のまま）
        ("team_game_stats", 22, 160),
        ("predictions", 19, 200),
        ("team_games", 10, 400),
        ("game_entries", 6, 640),
        ("venues", 7, 560),
        ("players", 5, 800),
        ("club_seasons", 8, 480),
        ("venue_source_keys", 2, 2000),
    ],
)
def test_limits_match_the_design_table(table: str, columns: int, limit: int) -> None:
    """詳細設計 3.4 の表と一致すること。表を手で書き換えたら落ちる。"""
    assert column_count(table) == columns
    assert max_rows_per_request(table) == limit


def test_limits_match_the_typescript_constants() -> None:
    """Python 側と `api/src/config/batch-limits.ts` が一致すること。

    どちらも DDL から数えた値であり、**写し間違いを許さない**。
    """
    source = (REPO_ROOT / "api" / "src" / "config" / "batch-limits.ts").read_text(encoding="utf-8")
    block = re.search(r"COLUMN_COUNTS[^{]*\{(.*?)\n\}", source, re.DOTALL)
    assert block, "COLUMN_COUNTS が見つからない"
    declared = dict(re.findall(r"(\w+):\s*(\d+)", block[1]))
    assert declared, "列数の定義が読めない"
    for table, value in declared.items():
        assert column_count(table) == int(value), table


def test_chunks_respect_the_request_limit() -> None:
    rows: list[dict[str, object]] = [{"i": index} for index in range(1000)]
    parts = chunks("games", rows)
    assert sum(len(part) for part in parts) == len(rows)
    assert all(len(part) <= max_rows_per_request("games") for part in parts)
    assert len(parts) == 1000 // 160 + 1
