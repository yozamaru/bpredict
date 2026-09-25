"""Official arena-detail URL construction, without response interpretation."""

import re
from urllib.parse import urlencode

from batch.scraper.client import ORIGIN, ConfigurationError


def arena_detail_url(venue_id: str | int) -> str:
    """会場の詳細ページ。**住所はここにしかない**（詳細設計 1.2 / 4.10）。

    収容人数はこのページにも無い（2026-09-25 に再確認した）。
    """
    if type(venue_id) not in (str, int) or re.fullmatch(r"[0-9]+", str(venue_id)) is None:
        raise ConfigurationError("Arena ID must contain decimal digits only.")
    if int(venue_id) <= 0:
        raise ConfigurationError("Arena ID must be positive.")
    return f"{ORIGIN}/arena_detail/?{urlencode({'ArenaCD': venue_id})}"
