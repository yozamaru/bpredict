"""国土地理院の住所検索。**1回だけ解決して CSV に固定する**（詳細設計 4.10）。

無料・APIキー不要で、政府標準利用規約のもとで提供されている（出典表記が条件）。
実行時（日次バッチ）には呼ばない。呼ぶのは `resolve_venue_geo` だけである。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

ENDPOINT = "https://msearch.gsi.go.jp/address-search/AddressSearch"

#: **運営者の判断として置いた値であり、実測に基づくものではない**（詳細設計 4.10）。
#: 公式サイト向けの3秒＋ジッタを無条件に適用しない。このジョブは1回しか流さず、
#: 総リクエスト数は会場数（120件前後）である。
REQUEST_INTERVAL_SECONDS = 1.0

TIMEOUT_SECONDS = 30.0
#: 住所1件の応答は数百バイト。桁違いの応答は読まずに落とす
MAX_RESPONSE_BYTES = 1024 * 1024

#: URL・応答本文・元の通信例外を持たない（絶対ルール4）
type Fetch = Callable[[str], bytes]


class GeocodeError(RuntimeError):
    """解決に失敗した。URL・応答本文・元の例外を含めない。"""


@dataclass(frozen=True)
class Candidate:
    title: str
    lat: float
    lng: float


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": "bpredict-geocode/1.0", "Accept-Encoding": "identity"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise GeocodeError(f"住所検索が非200を返した: {response.status:d}")
            return bytes(response.read(MAX_RESPONSE_BYTES + 1))
    except GeocodeError:
        raise
    except (urllib.error.URLError, OSError, ValueError) as error:
        # 元の例外を連ねない（URL が入る）
        raise GeocodeError(f"住所検索に到達できない（{type(error).__name__}）") from None


def candidates(address: str, *, fetch: Fetch | None = None) -> list[Candidate]:
    """住所の候補を**関連度順のまま**返す。どれを採るかは呼び出し側が決める。

    候補の都道府県が住所と食い違うことがあるため、1件目を無条件に採らない
    （詳細設計 4.10）。
    """
    if type(address) is not str or not address.strip():
        raise GeocodeError("住所が空である")
    url = f"{ENDPOINT}?{urllib.parse.urlencode({'q': address})}"
    body = (fetch or _fetch)(url)
    if len(body) > MAX_RESPONSE_BYTES:
        raise GeocodeError("住所検索の応答が大きすぎる")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise GeocodeError("住所検索の応答が JSON ではない") from None
    if not isinstance(payload, list):
        raise GeocodeError("住所検索の応答が配列ではない")
    found: list[Candidate] = []
    for item in payload:
        if not isinstance(item, dict):
            raise GeocodeError("住所検索の候補がオブジェクトではない")
        geometry = item.get("geometry")
        properties = item.get("properties")
        if not isinstance(geometry, dict) or not isinstance(properties, dict):
            raise GeocodeError("住所検索の候補に geometry か properties がない")
        coordinates = geometry.get("coordinates")
        if (not isinstance(coordinates, list) or len(coordinates) != 2
                or not all(type(v) in (int, float) for v in coordinates)):
            raise GeocodeError("住所検索の候補の座標が読めない")
        lng, lat = float(coordinates[0]), float(coordinates[1])
        # 日本の範囲。外れたら採らない（別の国の座標を入れない）
        if not (20.0 <= lat <= 46.0 and 122.0 <= lng <= 154.0):
            raise GeocodeError("住所検索の候補の座標が日本の範囲外")
        title = properties.get("title")
        if type(title) is not str:
            raise GeocodeError("住所検索の候補に title がない")
        found.append(Candidate(title=title, lat=lat, lng=lng))
    return found


def sleep_between_requests(sleep: Callable[[float], None] = time.sleep) -> None:
    sleep(REQUEST_INTERVAL_SECONDS)
