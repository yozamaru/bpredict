"""Official schedule URL construction, without response interpretation."""

from urllib.parse import urlencode

from batch.scraper.client import ORIGIN, ConfigurationError


def _parameters(year: int, month: str | int) -> dict[str, str | int]:
    if type(year) is not int or not 2016 <= year <= 9999:
        raise ConfigurationError("A B.LEAGUE season start year is required.")
    if month != "all" and (
        type(month) not in (str, int)
        or str(month) not in {str(value) for value in range(1, 13)}
    ):
        raise ConfigurationError("Month must be all or a calendar month.")
    return {"year": year, "mon": month, "tab": 1}


def schedule_html_url(year: int, month: str | int = "all") -> str:
    return f"{ORIGIN}/schedule/?{urlencode(_parameters(year, month))}"


def schedule_url(year: int, event: int, index: int, month: str | int = "all") -> str:
    if type(event) is not int or event not in (2, 3):
        raise ConfigurationError("Only regular season and playoff events are supported.")
    if type(index) is not int or index < 0:
        raise ConfigurationError("Schedule index must be a nonnegative integer.")
    parameters: dict[str, str | int] = {"data_format": "json", **_parameters(year, month)}
    parameters.update(event=event, index=index)
    return f"{ORIGIN}/schedule/?{urlencode(parameters)}"
