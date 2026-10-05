"""Official roster URL construction, without response interpretation."""

import re
from urllib.parse import urlencode

from batch.scraper.client import ORIGIN, ConfigurationError


def roster_url(year: int, club_id: str | int) -> str:
    """One club's registered players for one season (詳細設計 1.2 / 4.13).

    `year` is the season start year, matching the first four characters of
    `seasons.label`. `club_id` is the official `TeamID`, the same identifier the
    schedule and box score use.
    """
    if type(year) is not int or not 2016 <= year <= 9999:
        raise ConfigurationError("A B.LEAGUE season start year is required.")
    if type(club_id) not in (str, int) or re.fullmatch(r"[0-9]+", str(club_id)) is None:
        raise ConfigurationError("Club ID must contain decimal digits only.")
    if int(club_id) <= 0:
        raise ConfigurationError("Club ID must be positive.")
    return f"{ORIGIN}/roster/?{urlencode({'year': year, 'club': club_id})}"
