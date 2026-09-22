"""Workers の `/internal/*` への読み書き。**D1 REST API を直接叩かない**（絶対ルール3）。

書き込みはすべてこの口を通す。Zod 検証・認可・tipoff ガード・`is_final` 保護という
関門を迂回する経路を作らないため。

例外にはトークンも URL のクエリ文字列も入れない（絶対ルール4）。public リポジトリの
Actions ログは全世界から読める。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass

#: D1 書き込み失敗は3回まで再試行する（基本設計 4.3）
MAX_ATTEMPTS = 3
RETRY_WAIT_SECONDS = 2.0
TIMEOUT_SECONDS = 60.0
#: 再試行して意味があるのは一時障害だけ。4xx は投げ直しても同じ
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class LoaderError(RuntimeError):
    """内部APIとのやり取りに失敗した。URL・本文・元例外を含めない。"""


class RejectedError(LoaderError):
    """API が入力を拒否した（4xx）。再試行しない。"""


@dataclass(frozen=True)
class Response:
    status: int
    body: str

    def data(self) -> object:
        try:
            payload = json.loads(self.body)
        except ValueError:
            raise LoaderError("内部APIの応答が JSON でない") from None
        if not isinstance(payload, dict):
            raise LoaderError("内部APIの応答がオブジェクトでない")
        return payload.get("data")


type Transport = Callable[[str, str, bytes | None, Mapping[str, str]], Response]


def _transport(
    url: str, method: str, body: bytes | None, headers: Mapping[str, str]
) -> Response:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return Response(response.status, response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:  # HTTPError も応答である
        return Response(error.code, error.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 — 元の通信例外を漏らさない境界（絶対ルール4）
        # URL とトークンが含まれうるため、型も本文も伝播させない
        raise LoaderError("内部APIへの接続に失敗した") from None


class InternalApi:
    """`/api/v1/internal/*` のクライアント。"""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: Transport = _transport,
        sleep: Callable[[float], None] = time.sleep,
        dry_run: bool = False,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise LoaderError("API_BASE_URL の形式が不正")
        if not token:
            raise LoaderError("INGEST_TOKEN が空")
        self._base = base_url.rstrip("/")
        self._token = token
        self._transport = transport
        self._sleep = sleep
        self._dry_run = dry_run
        #: dry-run で送るはずだった件数を記録する（本番D1に書かずに確認するため）
        self.skipped: list[tuple[str, int]] = []

    def _url(self, path: str, query: Mapping[str, str] | None = None) -> str:
        url = f"{self._base}/api/v1/internal/{path.lstrip('/')}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return url

    def _send(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> object:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        headers = {"Authorization": f"Bearer {self._token}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        url = self._url(path, query)

        for attempt in range(1, MAX_ATTEMPTS + 1):
            response = self._transport(url, method, body, headers)
            if 200 <= response.status < 300:
                return response.data()
            if response.status in RETRYABLE_STATUS and attempt < MAX_ATTEMPTS:
                self._sleep(RETRY_WAIT_SECONDS * attempt)
                continue
            if 400 <= response.status < 500:
                # 応答本文を例外に入れない。件数とパスの種類だけで原因は追える
                raise RejectedError(f"内部APIが入力を拒否した（{response.status} / {path}）")
            raise LoaderError(f"内部APIが失敗を返した（{response.status} / {path}）")
        raise LoaderError(f"内部APIが再試行の上限に達した（{path}）")

    def post(self, path: str, payload: Mapping[str, object]) -> object:
        """書き込み。`dry_run` では送らずに件数だけ記録する。"""
        if self._dry_run:
            rows = sum(len(v) for v in payload.values() if isinstance(v, list))
            self.skipped.append((path, rows))
            return None
        return self._send("POST", path, payload=payload)

    def get(self, path: str, query: Mapping[str, str] | None = None) -> object:
        """運用上の読み取り。**入力データの取得には使わない**（絶対ルール3）。"""
        return self._send("GET", path, query=query)

    def ingested_game_ids(self, season_id: str) -> set[str]:
        """backfill の再開判定（詳細設計 4.8）。"""
        data = self.get("games/ingested", {"seasonId": season_id})
        if not isinstance(data, dict):
            raise LoaderError("取得済みIDの応答が不正")
        ids = data.get("gameIds")
        if not isinstance(ids, list):
            raise LoaderError("取得済みIDの応答に gameIds がない")
        return {str(value) for value in ids}
