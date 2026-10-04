"""日程ページを終端まで辿る（`backfill` と `daily_ingest` が共有する）。

**`jobs/` に置く。** 取得（`scraper/`）と解釈（`parser/`）の組み合わせであり、
`scraper/` はレスポンス本文を解釈しない（CLAUDE.md「責務の分離」）。
**2つのジョブが同じ歩き方をするため、1か所に置く** — 片方だけ直すと、
取り込みと日次で見える試合が違うことになる。

**飛ばした行の集計は呼び出し側が持つ。** ジョブごとに出力の形が違うため、
ページを1枚読むたびに `on_page` へ渡して、折り込み方は任せる。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping

from batch.parser.schedule_parser import ScheduleGame, SchedulePage, parse_schedule
from batch.scraper.client import RateLimitedClient
from batch.scraper.schedule import schedule_url


def walk_schedule(
    client: RateLimitedClient,
    *,
    year: int,
    event: int,
    clubs_by_name: Mapping[str, str],
    on_page: Callable[[SchedulePage], None],
    month: str | int = "all",
    stop: Callable[[SchedulePage], bool] | None = None,
) -> Iterator[ScheduleGame]:
    """終端まで日程ページを辿る。空の `topics` と `index=null` が終端。

    **`index` の前進を必須にしない。** 2016-17 のチャンピオンシップは
    15試合 / `index=null` の単一ページで、前進を必須にした実装は**CSを1件も
    取り込めなかった**（詳細設計 4.4）。

    **`month` を日次の取り込みに使わない（2026-10-05 の実測）。** 設計は「当月だけを
    辿る」ことを想定し、絞りが効かなくても「ページ数が増えるだけ」と見込んでいたが、
    **実際は読める行を1つも返さなかった** — `mon=10` は行0件（日付不明2件）で、
    `mon=all` は20件/ページで正しく返る。**想定のどちらでもなかった。**

    代わりに `stop` を使う。**ページは日付の昇順である**ため、ページの最終日が
    窓の終わりを超えたら以降は要らない（詳細設計 4.2 のステップ1b）。
    `stop` は `on_page` の後・次の取得の前に呼ぶ。
    """
    index = 0
    previous_date: str | None = None
    while True:
        page = parse_schedule(
            client.get(schedule_url(year, event, index, month)),
            year=year,
            event=event,
            clubs_by_name=clubs_by_name,
            previous_date=previous_date,
            index=index,
        )
        yield from page.games
        on_page(page)
        previous_date = page.last_date
        if page.next_index is None:
            return
        # **次を取る前に判定する。** 取得してから捨てるのは無駄な1リクエストである
        if stop is not None and stop(page):
            return
        index = page.next_index
