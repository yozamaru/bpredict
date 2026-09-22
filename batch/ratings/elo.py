"""Elo レーティングの算出（詳細設計 2.5）。

**入力はスナップショットの `games` と `seasons` だけ。** D1 も HTTP も知らない
（CLAUDE.md 絶対ルール3）。このモジュールは純粋な計算であり、書き出しは
`batch/jobs/recompute_ratings.py` が行う。

**差分更新をしない。** 対象期間を再計算して洗い替える（CLAUDE.md 冪等性）。
`recompute()` は常に全期間を replay する — 途中から始めると開始状態を
保存済みの値から拾うことになり、丸め差が世代を追って蓄積する。8,000試合の
replay は数十ミリ秒で終わるため、部分再計算に価値がない。書き込み範囲を
絞りたい場合は、出力を日付で切ってから送る（呼び出し側の仕事）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from batch.ratings.params import (
    HOME_ADVANTAGE_INITIAL,
    K_INITIAL,
    LEAGUE_MEAN,
    MARGIN_MULTIPLIER_ELO_WEIGHT,
    MARGIN_MULTIPLIER_SCALE,
    PROMOTED_ELO_INITIAL,
    SEASON_REGRESSION_INITIAL,
)

#: 出力の列。`team_ratings` の DDL と同じ順序（詳細設計 1.4）
RATING_COLUMNS = (
    "club_id",
    "as_of_date",
    "season_id",
    "elo",
    "off_rating",
    "def_rating",
    "pace",
    "games_played",
)


class RatingError(RuntimeError):
    """入力に必要な列や行が欠けている。"""


@dataclass(frozen=True)
class EloParams:
    """探索対象のパラメータ（詳細設計 2.5）。既定値は探索の初期値。"""

    k: float = K_INITIAL
    home_advantage: float = HOME_ADVANTAGE_INITIAL
    season_regression: float = SEASON_REGRESSION_INITIAL
    promoted_elo: float = PROMOTED_ELO_INITIAL
    league_mean: float = LEAGUE_MEAN


#: 探索前の既定。`EloParams` は frozen なので共有してよい
DEFAULT_PARAMS = EloParams()


def _as_int(value: object) -> int | None:
    """Parquet / SQLite / pandas が返す雑多な数値表現を int か None に落とす。

    pandas の欠損は `None` / `nan` / `pd.NA` のいずれでも来る。**0 と欠損を
    混同しない**（詳細設計 4.4 の正規化3と同じ理由）。
    """
    if value is None:
        return None
    text = str(value)
    if text in ("", "nan", "NaN", "None", "<NA>", "NaT"):
        return None
    return int(float(text))


def expected_home(elo_home: float, elo_away: float, home_advantage: float) -> float:
    """ホームの期待勝率。"""
    return float(1.0 / (1.0 + 10.0 ** ((elo_away - elo_home - home_advantage) / 400.0)))


def home_advantage_for(spectator_restricted: object, params: EloParams) -> float:
    """観客制限下のホームアドバンテージ（詳細設計 2.5）。

    `spectator_restricted` は 1 / 0 / NULL の3値である。**NULL は「制限されていた
    証拠がない」であって「制限されていた」ではない**ため、通常のホームアドバンテージ
    を使う。1 のときだけ 0 にする（`test_elo_uses_home_advantage_when_restriction_unknown`）。
    """
    if _as_int(spectator_restricted) == 1:
        return 0.0
    return params.home_advantage


def margin_multiplier(elo_home: float, elo_away: float, margin: int) -> float:
    """得点差による倍率。**Elo 差は試合前の値で評価する。**"""
    return math.log(margin + 1) * (
        MARGIN_MULTIPLIER_SCALE
        / (MARGIN_MULTIPLIER_ELO_WEIGHT * abs(elo_home - elo_away) + MARGIN_MULTIPLIER_SCALE)
    )


def rating_change(
    elo_home: float,
    elo_away: float,
    home_score: int,
    away_score: int,
    *,
    home_advantage: float,
    params: EloParams,
) -> float:
    """ホーム側の増分。アウェイは同量を引く（**零和**）。"""
    expected = expected_home(elo_home, elo_away, home_advantage)
    actual = 1.0 if home_score > away_score else 0.0
    multiplier = margin_multiplier(elo_home, elo_away, abs(home_score - away_score))
    return params.k * multiplier * (actual - expected)


def season_start_elo(previous_end: float | None, params: EloParams) -> float:
    """シーズン開始時の値（詳細設計 2.5）。

    `previous_end` が None のとき、それは「**前季にトップリーグの試合がない**」
    ことを意味する。空白が1シーズンでも8シーズンでも同じ扱いにし、過去に
    トップリーグにいた Elo が残っていても**持ち越さず捨てる**（要件 6.6）。

    `SEASON_REGRESSION` を空白シーズン数だけ適用すると偏差が `R^N` に縮んで
    実質リーグ平均になり、二部に長くいたクラブを平均と評価することになる。
    """
    if previous_end is None:
        return params.promoted_elo
    return params.league_mean + (previous_end - params.league_mean) * params.season_regression


def _ordered_seasons(games: pd.DataFrame, seasons: pd.DataFrame) -> list[str]:
    """試合があるシーズンを `start_date` 順に並べる。"""
    for column in ("id", "start_date"):
        if column not in seasons.columns:
            raise RatingError(f"seasons に {column} がない")
    present = set(games["season_id"])
    rows = seasons[seasons["id"].isin(present)].sort_values(["start_date", "id"])
    if len(rows) != len(present):
        missing = sorted(present - set(rows["id"]))
        raise RatingError(f"試合があるのに seasons にないシーズン: {missing}")
    return [str(value) for value in rows["id"]]


def _played(games: pd.DataFrame) -> pd.DataFrame:
    """Elo を動かすのは**結果が確定した試合だけ**。

    `SCHEDULED` / `POSTPONED` / `CANCELLED` を含めると、`home_score` が NULL の
    まま敗戦扱いされるか例外になる（CLAUDE.md 絶対ルール1の規約3と同じ理由）。
    """
    required = (
        "id", "season_id", "game_date", "tipoff_at", "status",
        "home_club_id", "away_club_id", "home_score", "away_score",
        "spectator_restricted",
    )
    for column in required:
        if column not in games.columns:
            raise RatingError(f"games に {column} がない")
    rows = games[
        (games["status"] == "FINISHED")
        & games["home_score"].notna()
        & games["away_score"].notna()
    ]
    # 同一日に同じクラブが2試合することはないため、日内の順序は結果に影響しない。
    # 決定論のためだけに並べる。
    return rows.sort_values(["game_date", "tipoff_at", "id"])


def recompute(
    games: pd.DataFrame,
    seasons: pd.DataFrame,
    *,
    params: EloParams = DEFAULT_PARAMS,
) -> pd.DataFrame:
    """全期間の Elo を再計算し、`team_ratings` の行を返す。

    **1行はその試合日の「終了時点」の値**である（詳細設計 1.4）。特徴量は
    `as_of_date < 対象試合日` の最新行を読むため、開始前の値を書くと前日の
    結果が永久に反映されない。同じ日に複数試合があるクラブは（制度上ないが）
    最後の試合まで含めた値で上書きする。

    `off_rating` / `def_rating` / `pace` は **NULL のままにする。** これらを使う
    特徴量（`ortg_diff` ほか）は検証区分であり、集計窓（当季通算か移動平均か、
    減衰を入れるか）が文書で定義されていない。工程8で区分を判断するときに
    決める（決め打ちで埋めると「実装しながら決めた値」が1つ増える）。
    """
    played = _played(games)
    if played.empty:
        return pd.DataFrame(columns=list(RATING_COLUMNS))

    order = _ordered_seasons(played, seasons)
    #: 各シーズン終了時点の値。次シーズンの開始値の算出にだけ使う
    season_end: dict[str, dict[str, float]] = {}
    rows: dict[tuple[str, str], tuple[object, ...]] = {}

    for index, season_id in enumerate(order):
        season_games = played[played["season_id"] == season_id]
        clubs = set(season_games["home_club_id"]) | set(season_games["away_club_id"])
        previous = season_end.get(order[index - 1], {}) if index > 0 else None

        elo: dict[str, float] = {}
        for club_id in clubs:
            if previous is None:
                # **データ上の最初のシーズン**。全クラブがリーグ平均から始まる。
                # ここで昇格扱い（1400）にすると、Elo は零和なのでリーグ平均が
                # 1400 に固定され、以後シーズン間回帰が毎年 1500 方向へ引っ張る。
                # 「昇格」は前季にトップリーグの実績がないことを指すのであって、
                # 手元にデータがないことを指すのではない。
                elo[str(club_id)] = params.league_mean
            else:
                elo[str(club_id)] = season_start_elo(previous.get(str(club_id)), params)

        games_played = dict.fromkeys(elo, 0)

        for game in season_games.itertuples(index=False):
            home, away = str(game.home_club_id), str(game.away_club_id)
            home_score, away_score = _as_int(game.home_score), _as_int(game.away_score)
            if home_score is None or away_score is None:
                raise RatingError("結果が確定した試合のスコアが欠けている")
            change = rating_change(
                elo[home],
                elo[away],
                home_score,
                away_score,
                home_advantage=home_advantage_for(game.spectator_restricted, params),
                params=params,
            )
            elo[home] += change
            elo[away] -= change
            games_played[home] += 1
            games_played[away] += 1
            for club_id in (home, away):
                rows[(club_id, str(game.game_date))] = (
                    club_id, str(game.game_date), season_id,
                    elo[club_id], None, None, None, games_played[club_id],
                )

        season_end[season_id] = dict(elo)

    frame = pd.DataFrame(list(rows.values()), columns=list(RATING_COLUMNS))
    return frame.sort_values(["as_of_date", "club_id"], ignore_index=True)
