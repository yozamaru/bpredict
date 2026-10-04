"""静的JSON の入力をスナップショットと推論の結果から組む（詳細設計 4.2 のステップ5）。

**ここが知っているのは「どこから取るか」だけである。** 形は `builder.py` が持ち、
ファイルの配置と削除は `writer.py` が持つ。

**予測は D1 から読み戻さない。** `daily_ingest` は 06:00 JST に走り、最も早い開始
時刻（14:05 JST）より前であるため、窓の全試合がその回で推論される（詳細設計 4.2）。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from batch.features.dataset import Dataset
from batch.static_json.builder import (
    ClubInput,
    GameDetailInput,
    GameInput,
    GameListInput,
    PredictionInput,
    ReasonInput,
)


class StaticInputError(ValueError):
    """入力を組めない。"""


@dataclass(frozen=True)
class PredictedGame:
    """推論が出した1試合ぶん。**静的JSON に必要な分だけを持つ。**"""

    game_id: str
    home_win_prob: float
    pred_home_score: float
    pred_away_score: float
    model_version: str
    #: 根拠（詳細設計 2.7.1）。**現在の21列では2件**（`VENUE` に該当列がなく、
    #: `PLAYER` の3列は定数で寄与が厳密に 0）。空のこともある
    reasons: tuple[ReasonInput, ...] = ()


def _clubs(ds: Dataset) -> dict[str, tuple[str, str]]:
    """`club_id` → (`slug`, `clubs.name`)。**表示名には使わない**（下記）。"""
    table = ds.table("clubs")
    return {str(r.id): (str(r.slug), str(r.name)) for r in table.itertuples()}


def _season_names(ds: Dataset) -> dict[tuple[str, str], tuple[str | None, str | None]]:
    """(`season_id`, `club_id`) → (`name`, `short_name`)。

    **表示名は年度断面から取る**（`clubs.name` は現在の表示名であり、過去試合に
    出すと遡って変わる。詳細設計 1.2）。**当季は最初の試合が終わるまで空**であり、
    そのときは None を返す — **`clubs.name` で埋めない**（既知の判断待ちであり、
    公開APIも null を返す）。
    """
    if "club_seasons" not in ds.tables:
        return {}
    table = ds.tables["club_seasons"]
    out: dict[tuple[str, str], tuple[str | None, str | None]] = {}
    for row in table.itertuples():
        name = _text(row.name)
        short = _text(row.short_name)
        out[(str(row.season_id), str(row.club_id))] = (name, short)
    return out


def _club_input(
    club_id: str, season_id: str,
    clubs: Mapping[str, tuple[str, str]],
    names: Mapping[tuple[str, str], tuple[str | None, str | None]],
) -> ClubInput:
    slug = clubs.get(club_id, (club_id, club_id))[0]
    name, short = names.get((season_id, club_id), (None, None))
    return ClubInput(club_id=club_id, slug=slug, name=name, short_name=short)


def _missing(value: object) -> bool:
    """単値の欠損判定。`batch/features/prepared.py` の `_is_missing` と同じ規約で、
    **空文字と欠損を混ぜない。**
    """
    if value is None:
        return True
    if isinstance(value, float):
        return math.isnan(value)
    return str(value) in ("nan", "NaT", "<NA>", "")


def _text(value: object) -> str | None:
    return None if _missing(value) else str(value)


def _score(value: object) -> int | None:
    """スコアは NULL か整数。**0 と欠損を混ぜない**（詳細設計 1.3）。"""
    return None if _missing(value) else int(float(str(value)))


def _days(today: str, days: int) -> list[str]:
    """翌日から `days` 日ぶんの暦日（当日は含めない）。"""
    start = datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=UTC)
    return [(start + timedelta(days=n)).strftime("%Y-%m-%d") for n in range(1, days + 1)]


def build_inputs(
    ds: Dataset, predictions: Sequence[PredictedGame], *, today: str, days: int,
) -> tuple[GameListInput, list[GameListInput], list[GameDetailInput]]:
    """`today.json` / `schedule/<date>.json` / `games/<id>.json` の入力。

    **試合が1件も無い日も返す。** 書かないと古い `today.json` が残り、昨日の試合が
    「今日の試合」として配信され続ける（詳細設計 4.2 のステップ5）。
    """
    clubs = _clubs(ds)
    names = _season_names(ds)
    by_id = {p.game_id: p for p in predictions}
    games = ds.table("games")
    wanted = [today, *_days(today, days)]
    picked = games[games["game_date"].isin(wanted)].sort_values(["tipoff_at", "id"])

    rows: dict[str, list[tuple[GameInput, PredictionInput | None]]] = {
        day: [] for day in wanted
    }
    details: list[GameDetailInput] = []
    for row in picked.itertuples():
        season_id = str(row.season_id)
        game = GameInput(
            game_id=str(row.id),
            tipoff_at=str(row.tipoff_at),
            status=str(row.status),
            competition=str(row.competition),
            home=_club_input(str(row.home_club_id), season_id, clubs, names),
            away=_club_input(str(row.away_club_id), season_id, clubs, names),
            league=_text(row.league),
            venue_name=_text(row.venue_name_at_game),
            home_score=_score(row.home_score),
            away_score=_score(row.away_score),
        )
        found = by_id.get(game.game_id)
        prediction = None if found is None else PredictionInput(
            home_win_prob=found.home_win_prob,
            pred_home_score=found.pred_home_score,
            pred_away_score=found.pred_away_score,
            # **エントリー情報を取得していないため常に暫定**（要件 F-06）
            is_provisional=True,
            is_final=False,
            model_version=found.model_version,
            # **推測で False を入れない。** 消化試合数の集計がない（builder の注記）
            is_early_season=None,
        )
        rows[str(row.game_date)].append((game, prediction))
        if str(row.game_date) == today:
            # **詳細は当日の試合だけ**（詳細設計 3.7。7日窓にすると年460MB 積む）
            details.append(GameDetailInput(
                game=game, prediction=prediction,
                reasons=[] if found is None else list(found.reasons),
            ))

    # **的中率は渡さない（null）。** `accuracy_summary` は D1 にあり読む口が無く、
    # そもそも1試合も照合していない間は存在しない（詳細設計 4.2 のステップ5）
    return (
        GameListInput(game_date=today, games=rows[today]),
        [GameListInput(game_date=day, games=rows[day]) for day in _days(today, days)],
        details,
    )
