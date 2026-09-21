"""揃っている値の恒等式を確認し、欠損から数値を捏造しない。"""

from .errors import ValidationError
from .fields import SCORING_FIELDS
from .models import Counts, PlayerStats


def validate_counts(stats: Counts, rebound_total: int | None) -> None:
    for made, attempted in ((stats.fg2m, stats.fg2a), (stats.fg3m, stats.fg3a),
                            (stats.ftm, stats.fta)):
        if made is not None and attempted is not None and made > attempted:
            raise ValidationError("シュート成功数が試投数を超える")
    if (stats.pts is not None and stats.fg2m is not None
            and stats.fg3m is not None and stats.ftm is not None
            and stats.pts != 2 * stats.fg2m + 3 * stats.fg3m + stats.ftm):
        raise ValidationError("得点の恒等式に違反")
    if (rebound_total is not None and stats.oreb is not None and stats.dreb is not None
            and rebound_total != stats.oreb + stats.dreb):
        raise ValidationError("リバウンドの恒等式に違反")


def validate_scoring_total(stats: Counts, players: list[PlayerStats]) -> None:
    for field in SCORING_FIELDS:
        expected: int | None = getattr(stats, field)
        values: list[int | None] = [getattr(p.stats, field) for p in players]
        if (expected is not None and all(v is not None for v in values)
                and sum(v for v in values if v is not None) != expected):
            raise ValidationError("チームと選手の得点・シュート合計が一致しない")


def possessions(stats: Counts) -> float | None:
    if (stats.fg2a is None or stats.fg3a is None or stats.oreb is None
            or stats.tov is None or stats.fta is None):
        return None
    result = stats.fg2a + stats.fg3a - stats.oreb + stats.tov + 0.44 * stats.fta
    if not 50 <= result <= 120:
        raise ValidationError("ポゼッションが値域外")
    return result
