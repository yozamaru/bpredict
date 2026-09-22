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


def validate_scoring_total(
    stats: Counts, players: list[PlayerStats], others: list[Counts] | None = None,
) -> None:
    """公式合計と、選手行 + `Category=2` の行の合計を照合する（詳細設計 4.4）。

    **`others`（`Category=2`）を分母に含める。** 得点を持つ `Category=2` 行が実在し、
    **公式合計にはその得点が入っている**。区分だけで一律に外すと、2016-17 の7試合が
    「合計が一致しない」として丸ごと落ちた。

        Cat=2 PlayerID=9335 PlayTime='DNP' Point=2 PT2M=1 PT2A=1
        公式合計 Point=85 / 選手合計 Point=83  ← この行のぶんだけ足りない
    """
    extra = others or []
    for field in SCORING_FIELDS:
        expected: int | None = getattr(stats, field)
        values: list[int | None] = [getattr(p.stats, field) for p in players]
        values += [getattr(c, field) for c in extra]
        if (expected is not None and all(v is not None for v in values)
                and sum(v for v in values if v is not None) != expected):
            raise ValidationError("チームと選手の得点・シュート合計が一致しない")


#: 通常の試合であればこの範囲に入る（詳細設計 1.3）
POSSESSION_RANGE = (50.0, 120.0)


def possessions(stats: Counts) -> float | None:
    """推定ポゼッション。算出できない場合と値域外はいずれも None。

    **値域外で例外を投げない。** 中断・不成立の短い試合が実在し（2016-17 の `1330` は
    川崎 26–18 A東京 / 31分）、例外にすると**試合そのものが落ちる**。スコアは公式記録で
    Elo に必要であり、ポゼッションは「通常の試合であれば」という前提の下の推定値である。
    **値が信用できないことと、試合がなかったことは別である**（詳細設計 1.3）。
    """
    if (stats.fg2a is None or stats.fg3a is None or stats.oreb is None
            or stats.tov is None or stats.fta is None):
        return None
    result = stats.fg2a + stats.fg3a - stats.oreb + stats.tov + 0.44 * stats.fta
    low, high = POSSESSION_RANGE
    return None if not low <= result <= high else result
