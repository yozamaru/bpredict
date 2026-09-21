"""Official box-score URL construction, without response interpretation."""

import re
from urllib.parse import urlencode

from batch.scraper.client import ORIGIN, ConfigurationError


def boxscore_url(game_id: str | int) -> str:
    if type(game_id) not in (str, int) or re.fullmatch(r"[0-9]+", str(game_id)) is None:
        raise ConfigurationError("Game ID must contain decimal digits only.")
    if int(game_id) <= 0:
        raise ConfigurationError("Game ID must be positive.")
    return f"{ORIGIN}/game_detail/?{urlencode({'ScheduleKey': game_id, 'tab': 2})}"
