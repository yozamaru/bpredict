"""会場の座標の解決（詳細設計 4.10）。合成データのみ。実サイトへは通信しない。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from batch.geocode.gsi import Candidate, GeocodeError, candidates
from batch.jobs import resolve_venue_geo
from batch.parser.arena_parser import parse_arena_address, prefecture_of
from batch.parser.errors import DataUnavailable, ParseError
from batch.scraper.arena import arena_detail_url
from batch.scraper.client import ConfigurationError, ScrapingStopped

# --- URL 構築 ---


def test_arena_url_uses_the_official_id() -> None:
    assert arena_detail_url(3) == "https://www.bleague.jp/arena_detail/?ArenaCD=3"
    assert arena_detail_url("291") == "https://www.bleague.jp/arena_detail/?ArenaCD=291"


@pytest.mark.parametrize("bad", ["", "3a", "-1", "0", " 3", None, 3.0])
def test_arena_url_rejects_anything_but_a_positive_id(bad: object) -> None:
    with pytest.raises(ConfigurationError):
        arena_detail_url(bad)  # type: ignore[arg-type]


# --- 住所の解析 ---

def arena_html(address: str = "千葉県架空市架空町1-2-3", *, heading: str = "住所") -> str:
    """実サイトの構造を模した合成データ（要件 4.5.2）。

    **都道府県名だけは実在の名前を使う。** 住所の先頭から都道府県を決める処理を
    検証するには実在の名前が必要で、都道府県名は地名（事実）であってサイトの文言
    ではない。市区町村以下はダミーである。
    """
    return f"""
    <div class="grid-col">
      <dl class="definition definition-icon">
        <dt class="definition-heading definition-heading-club">拠点クラブ</dt>
        <dd class="definition-content">架空クラブ</dd>
      </dl>
      <dl class="definition definition-icon">
        <dt class="definition-heading definition-heading-address">{heading}</dt>
        <dd class="definition-content">{address}</dd>
      </dl>
    </div>
    """


def test_address_is_read_with_its_prefecture() -> None:
    found = parse_arena_address(arena_html("千葉県架空市架空台7-5-1"))
    assert found.address == "千葉県架空市架空台7-5-1"
    assert found.prefecture == "千葉県"


def test_other_definition_rows_are_not_read_as_the_address() -> None:
    """**同じ `dd.definition-content` が他の項目にも使われている。**

    見出しの class（`definition-heading-address`）で選ばないと、拠点クラブ名を
    住所として読む。
    """
    assert parse_arena_address(arena_html()).address != "架空クラブ"


def test_missing_address_is_data_unavailable_not_a_parse_error() -> None:
    """住所の欄がない会場は**飛ばして報告する**（構造変更とは区別する）。"""
    html = arena_html().replace("definition-heading-address", "definition-heading-other")
    with pytest.raises(DataUnavailable):
        parse_arena_address(html)


def test_conflicting_addresses_are_a_parse_error() -> None:
    """住所が2つあって食い違うのは構造が変わった疑い。どちらかを選ばない。"""
    html = arena_html("千葉県架空A市1-1") + arena_html("千葉県架空B市2-2")
    with pytest.raises(ParseError):
        parse_arena_address(html)


def test_the_same_address_twice_is_accepted() -> None:
    html = arena_html("千葉県架空A市1-1") + arena_html("千葉県架空A市1-1")
    assert parse_arena_address(html).address == "千葉県架空A市1-1"


def test_fullwidth_digits_are_normalized() -> None:
    assert parse_arena_address(arena_html("東京都架空区１−２−３")).address.startswith("東京都")


def test_unknown_prefecture_is_none_not_guessed() -> None:
    assert parse_arena_address(arena_html("架空県架空市1-1")).prefecture is None  # 実在しない県名
    assert prefecture_of("Chiba, Japan") is None


def test_all_47_prefectures_are_recognized() -> None:
    from batch.parser.arena_parser import PREFECTURES

    assert len(set(PREFECTURES)) == 47
    for name in PREFECTURES:
        assert prefecture_of(f"{name}架空市1-1") == name


# --- 住所検索（国土地理院） ---

def gsi_body(items: list[tuple[str, float, float]]) -> bytes:
    return json.dumps([
        {"geometry": {"coordinates": [lng, lat], "type": "Point"},
         "type": "Feature", "properties": {"addressCode": "", "title": title}}
        for title, lat, lng in items
    ]).encode()


def test_candidates_are_returned_in_the_order_given() -> None:
    body = gsi_body([("千葉県架空A市", 35.1, 139.1), ("千葉県架空B市", 36.2, 140.2)])
    found = candidates("千葉県架空A市1-1", fetch=lambda _url: body)
    assert [c.title for c in found] == ["千葉県架空A市", "千葉県架空B市"]
    assert (found[0].lat, found[0].lng) == (35.1, 139.1)


def test_coordinates_outside_japan_are_rejected() -> None:
    """**別の国の座標を入れない。** 住所検索が国外を返したら採らない。"""
    body = gsi_body([("Somewhere", 51.5, -0.12)])
    with pytest.raises(GeocodeError):
        candidates("千葉県架空A市1-1", fetch=lambda _url: body)


@pytest.mark.parametrize("body", [
    b"not json", b"{}", b'[{"geometry":{}}]', b'[{"geometry":{"coordinates":[1]},"properties":{}}]',
    b'[{"geometry":{"coordinates":[139.1,35.1]},"properties":{}}]',
])
def test_malformed_geocoding_responses_fail_closed(body: bytes) -> None:
    with pytest.raises(GeocodeError):
        candidates("千葉県架空A市1-1", fetch=lambda _url: body)


def test_empty_address_is_rejected_before_any_request() -> None:
    def boom(_url: str) -> bytes:
        raise AssertionError("通信してはいけない")

    with pytest.raises(GeocodeError):
        candidates("   ", fetch=boom)


def test_geocode_errors_never_leak_the_url() -> None:
    body = gsi_body([("Somewhere", 51.5, -0.12)])
    with pytest.raises(GeocodeError) as error:
        candidates("千葉県架空A市1-1", fetch=lambda _url: body)
    assert "msearch" not in str(error.value)
    assert "http" not in str(error.value).lower()


# --- 候補の選び方 ---

def test_candidate_with_a_different_prefecture_is_not_taken() -> None:
    """**1件目を無条件に採らない。** 別の都道府県の座標が入りうる（詳細設計 4.10）。"""
    found = [Candidate("東京都架空C区", 35.0, 139.0), Candidate("千葉県架空A市", 36.0, 140.0)]
    assert resolve_venue_geo.pick(found, "千葉県").lat == 36.0


def test_no_matching_prefecture_raises() -> None:
    with pytest.raises(GeocodeError, match="食い違う"):
        resolve_venue_geo.pick([Candidate("東京都架空C区", 35.0, 139.0)], "千葉県")


def test_no_candidates_raises() -> None:
    with pytest.raises(GeocodeError, match="候補を返さなかった"):
        resolve_venue_geo.pick([], "架空県")


def test_unknown_prefecture_raises_instead_of_taking_the_first() -> None:
    with pytest.raises(GeocodeError, match="都道府県が読めない"):
        resolve_venue_geo.pick([Candidate("千葉県架空A市", 35.0, 139.0)], None)


# --- ジョブ ---

class FakeScraper:
    def __init__(self, bodies: dict[str, Any]) -> None:
        self.bodies = bodies
        self.requested: list[str] = []
        self.verified = 0

    def verify_policy(self, terms_reporter: object = None) -> None:
        self.terms_reporter = terms_reporter
        self.verified += 1

    def get(self, url: str) -> str:
        self.requested.append(url)
        for key, value in self.bodies.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return str(value)
        raise AssertionError(f"想定外の取得: {url}")


class FakeApi:
    def __init__(self, venues: list[dict[str, Any]]) -> None:
        self._venues = venues
        self.posted: list[tuple[str, dict[str, Any]]] = []

    def get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        assert path == "venues"
        assert params == {"missingCoordinates": "1"}
        return {"count": len(self._venues), "venues": self._venues}

    def post(self, path: str, payload: dict[str, Any]) -> None:
        self.posted.append((path, payload))


def run(
    venues: list[dict[str, Any]], bodies: dict[str, Any], tmp_path: Path,
    *, geocode: object = None, dry_run: bool = False,
) -> tuple[resolve_venue_geo.Result, FakeScraper, FakeApi, Path]:
    scraper, api = FakeScraper(bodies), FakeApi(venues)
    path = tmp_path / "venues_geo.csv"
    result = resolve_venue_geo.resolve(
        client=scraper,  # type: ignore[arg-type]
        api=api,  # type: ignore[arg-type]
        geocode=geocode or (lambda _a: [Candidate("千葉県架空市", 35.5, 139.5)]),  # type: ignore[arg-type]
        sleep=lambda: None,
        path=path,
        dry_run=dry_run,
    )
    return result, scraper, api, path


def test_resolves_and_writes_the_csv(tmp_path: Path) -> None:
    result, scraper, _api, path = run(
        [{"id": "3", "name": "架空アリーナ"}], {"ArenaCD=3": arena_html()}, tmp_path)

    assert (result.resolved, result.kept, result.skipped) == (1, 0, [])
    assert scraper.verified == 1, "取得前確認を通る（robots / 利用規約）"
    rows = resolve_venue_geo.read_csv(path)
    assert rows["3"]["lat"] == "35.500000"
    assert rows["3"]["prefecture"] == "千葉県"
    assert rows["3"]["address"] == "千葉県架空市架空町1-2-3"
    assert "国土地理院" in rows["3"]["source"], "出典を必ず残す（政府標準利用規約）"


def test_already_resolved_venues_are_not_fetched_again(tmp_path: Path) -> None:
    """**1回だけ解決する。** CSV に座標がある会場は取りに行かない。"""
    path = tmp_path / "venues_geo.csv"
    resolve_venue_geo.write_csv({"3": {
        "venue_id": "3", "name": "架空アリーナ", "prefecture": "千葉県",
        "lat": "35.000000", "lng": "139.000000", "address": "千葉県架空市1-1", "source": "x",
    }}, path)
    scraper, api = FakeScraper({}), FakeApi([{"id": "3", "name": "架空アリーナ"}])
    result = resolve_venue_geo.resolve(
        client=scraper,  # type: ignore[arg-type]
        api=api,  # type: ignore[arg-type]
        geocode=lambda _a: [],
        sleep=lambda: None,
        path=path,
    )
    assert (result.resolved, result.kept) == (0, 1)
    assert scraper.requested == []


def test_a_venue_without_an_address_is_skipped_with_its_id(tmp_path: Path) -> None:
    """**件数だけでは調査ができない**（詳細設計 4.4 と同じ方針）。"""
    html = arena_html().replace("definition-heading-address", "definition-heading-other")
    result, _scraper, _api, path = run(
        [{"id": "3", "name": "架空アリーナ"}], {"ArenaCD=3": html}, tmp_path)

    assert result.resolved == 0
    assert len(result.skipped) == 1
    venue_id, reason = result.skipped[0]
    assert venue_id == "3"
    assert "DataUnavailable" in reason
    assert not path.exists(), "1件も解決していないなら CSV を書き換えない"


def test_one_failure_does_not_stop_the_others(tmp_path: Path) -> None:
    result, _scraper, _api, path = run(
        [{"id": "3", "name": "架空A"}, {"id": "4", "name": "架空B"}],
        {"ArenaCD=3": arena_html().replace("definition-heading-address", "x"),
         "ArenaCD=4": arena_html()},
        tmp_path)

    assert result.resolved == 1
    assert [v for v, _ in result.skipped] == ["3"]
    assert set(resolve_venue_geo.read_csv(path)) == {"4"}


def test_geocoding_failure_is_reported_not_guessed(tmp_path: Path) -> None:
    result, _scraper, _api, _path = run(
        [{"id": "3", "name": "架空A"}], {"ArenaCD=3": arena_html()}, tmp_path,
        geocode=lambda _a: [])

    assert result.resolved == 0
    assert "GeocodeError" in result.skipped[0][1]


def test_scraping_stop_ends_the_job_with_a_note(tmp_path: Path) -> None:
    """429 / 503 は取得区間のみ中止する（基本設計 4.3）。"""
    result, _scraper, _api, _path = run(
        [{"id": "3", "name": "架空A"}, {"id": "4", "name": "架空B"}],
        {"ArenaCD=3": ScrapingStopped("停止"), "ArenaCD=4": arena_html()},
        tmp_path)

    assert result.resolved == 0
    assert result.notes == ["429/503 により取得区間を中止した"]


def test_dry_run_does_not_write_the_csv(tmp_path: Path) -> None:
    result, _scraper, _api, path = run(
        [{"id": "3", "name": "架空A"}], {"ArenaCD=3": arena_html()}, tmp_path, dry_run=True)
    assert result.resolved == 1
    assert not path.exists()


def test_load_sends_the_csv_to_the_internal_api(tmp_path: Path) -> None:
    path = tmp_path / "venues_geo.csv"
    resolve_venue_geo.write_csv({
        "3": {"venue_id": "3", "name": "架空A", "prefecture": "千葉県",
              "lat": "35.100000", "lng": "139.100000", "address": "千葉県架空A市1-1", "source": "x"},
        "4": {"venue_id": "4", "name": "架空B", "prefecture": "",
              "lat": "", "lng": "", "address": "", "source": ""},
    }, path)
    api = FakeApi([])
    assert resolve_venue_geo.load(api, path) == 2  # type: ignore[arg-type]

    path_sent, payload = api.posted[0]
    assert path_sent == "games", "会場は POST /internal/games の venues 配列で受ける"
    sent = {v["id"]: v for v in payload["venues"]}
    assert sent["3"]["lat"] == 35.1
    # **空欄は 0 ではなく NULL。** 0 を送ると赤道上の座標が入る
    assert sent["4"]["lat"] is None and sent["4"]["prefecture"] is None


def test_duplicate_rows_in_the_csv_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "venues_geo.csv"
    path.write_text(
        "venue_id,name,prefecture,lat,lng,address,source\n"
        "3,A,千葉県,35.1,139.1,千葉県架空A市1-1,x\n"
        "3,B,千葉県,36.1,140.1,千葉県架空B市2-2,x\n", encoding="utf-8")
    with pytest.raises(Exception, match="重複"):
        resolve_venue_geo.read_csv(path)
