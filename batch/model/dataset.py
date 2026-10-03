"""学習行列の構築（基本設計 2.3）。入力はスナップショットだけである。

**`as_of` は必ず `games.tipoff_at` にする。** ここで別の時刻を渡すと、リークテストが
通っているのに学習だけがリークするという最悪の形になる。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd
from numpy.typing import NDArray

from batch.features import team_rate
from batch.features.builder import FEATURE_KEYS, build_features
from batch.features.dataset import Dataset
from batch.features.prepared import prepare
from batch.model.params import TIME_DECAY_LAMBDA_INITIAL

type Floats = NDArray[np.float64]


class MatrixError(ValueError):
    """学習行列を組めない入力。"""


@dataclass(frozen=True)
class TrainingData:
    """時系列順に並んだ学習行列。**シャッフルしない**（要件 6.3）。"""

    features: pd.DataFrame
    #: ホーム勝利なら1
    home_win: Floats
    #: ホーム得点 − アウェイ得点
    margin: Floats
    #: 合計得点
    total: Floats
    game_ids: list[str]
    season_ids: list[str]
    #: その試合の JST 暦日（並び順の確認用）
    game_dates: list[str]
    #: 観客制限（1 / 0 / NULL）。**特徴量ではない。** 採否が定まっていないため
    #: 列として運び、重みにも特徴量にも使わない（下記）
    spectator_restricted: list[float | None]

    def __len__(self) -> int:
        return len(self.game_ids)

    @property
    def seasons(self) -> list[str]:
        """出現順のシーズン。**並べ替えない** — 時系列分割の順序がこれで決まる。"""
        seen: list[str] = []
        for season in self.season_ids:
            if season not in seen:
                seen.append(season)
        return seen


def _as_of(value: object) -> datetime:
    text = str(value)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        raise MatrixError("tipoff_at を時刻として読めない") from None


def build_matrix(ds: Dataset) -> TrainingData:
    """終了した試合すべてについて特徴量と目的変数を組む。

    **スコアが欠けている `FINISHED` の試合は落とす。** 0 として扱うと勝敗が反転し、
    Elo と学習の両方が壊れる（値域検証を通っていれば起こらないが、ここでも守る）。
    """
    games = ds.table("games")
    finished = games[games["status"] == "FINISHED"].copy()
    if finished.empty:
        raise MatrixError("終了した試合が1件もない")
    finished = finished.sort_values(["tipoff_at", "id"], kind="stable")

    # **索引は1回だけ組む。** 試合ごとに組み直すと 6,270試合で43分かかる
    # （2026-10-02 の実測）。索引を渡しても値は変わらない — `as_of` の
    # 絞り込みは `build_context` が行う（`batch/features/prepared.py`）
    prepared = prepare(ds)

    rows: list[dict[str, float]] = []
    home_win: list[float] = []
    margin: list[float] = []
    total: list[float] = []
    game_ids: list[str] = []
    season_ids: list[str] = []
    game_dates: list[str] = []
    restricted: list[float | None] = []

    for game in finished.itertuples():
        if pd.isna(game.home_score) or pd.isna(game.away_score):
            continue
        # pandas の itertuples は列の型を Any にしないため、ここで数値へ寄せる
        home = float(str(game.home_score))
        away = float(str(game.away_score))
        rows.append(build_features(
            str(game.id), as_of=_as_of(game.tipoff_at), ds=ds, prepared=prepared))
        home_win.append(1.0 if home > away else 0.0)
        margin.append(home - away)
        total.append(home + away)
        game_ids.append(str(game.id))
        season_ids.append(str(game.season_id))
        game_dates.append(str(game.game_date))
        value = getattr(game, "spectator_restricted", None)
        restricted.append(None if pd.isna(value) else float(value))

    if not rows:
        raise MatrixError("スコアの揃った終了試合が1件もない")
    features = pd.DataFrame(rows, columns=list(FEATURE_KEYS))
    return TrainingData(
        features=features,
        home_win=np.asarray(home_win, dtype=np.float64),
        margin=np.asarray(margin, dtype=np.float64),
        total=np.asarray(total, dtype=np.float64),
        game_ids=game_ids,
        season_ids=season_ids,
        game_dates=game_dates,
        spectator_restricted=restricted,
    )


def time_decay_weights(
    season_ids: list[str], seasons: list[str], *,
    lam: float = TIME_DECAY_LAMBDA_INITIAL,
) -> Floats:
    """`weight = exp(-λ × 経過シーズン数)`（要件 6.6）。

    経過は `seasons` の末尾（最新）からの距離で数える。10年前の試合と先週の試合を
    同じ重みで学習しないための措置であり、ルール変更とリーグ再編の両方に効く。

    **λ は探索の対象である**（`TIME_DECAY_LAMBDA_GRID`）。ここでの既定値は出発点で
    あって確定値ではない。
    """
    if lam < 0:
        raise MatrixError("時間減衰の λ が負である")
    order = {season: index for index, season in enumerate(seasons)}
    newest = len(seasons) - 1
    unknown = [s for s in season_ids if s not in order]
    if unknown:
        raise MatrixError("シーズンの一覧にない試合がある")
    age = np.asarray([newest - order[s] for s in season_ids], dtype=np.float64)
    return np.exp(-lam * age)


def constant_columns(features: pd.DataFrame) -> list[str]:
    """分散が0の列。**採用ゲートの欠損率では捕まらない**（2026-09-25 に実測）。

    `minutes_lost_diff` / `top_players_out_diff` / `entry_is_official` は
    `game_entries` が空のとき**定数0**になる。NULL ではないため
    `feature_null_rates` は 0% と出て、「実装したのに効いていない特徴量」が
    静かに残る。LightGBM は分割に使えないため予測は壊れないが、**効いていない
    ことを知らないまま採用判定を通すのは別の問題である。**
    """
    return [str(name) for name in features.columns if features[name].nunique(dropna=False) <= 1]


def null_rates(features: pd.DataFrame) -> dict[str, float]:
    """列ごとの欠損率（詳細設計 4.6 の `feature_null_rates`）。"""
    return {str(name): float(features[name].isna().mean()) for name in features.columns}


# --- TeamRate（チーム目標モデル）の学習行列（詳細設計 2.2.1） ---


@dataclass(frozen=True)
class TeamRateData:
    """**1行は「1試合 × 1クラブ」。** 1試合から2行できる。

    14本のモデルが共有する1枚の表として持つ。`features(target)` が案Cの5列を
    切り出す（`batch/features/team_rate.py`）。**14枚を別々に作らない** —
    `Context` を14回組み直すことになり、しかも推論側は14項目を1組で要る。
    """

    #: 共有3列 + 目的変数ごと2列 × 14 = 31列
    rows: pd.DataFrame
    #: 目的変数（14列）。その試合でそのクラブが実際に記録した値
    actual: pd.DataFrame
    #: 成功率3項目の学習重み（試投数）。列名は `fg2a` / `fg3a` / `fta`
    attempts: pd.DataFrame
    game_ids: list[str]
    club_ids: list[str]
    season_ids: list[str]
    game_dates: list[str]
    #: そのクラブが勝ったなら1（クラブ視点。ホーム視点ではない）
    club_win: Floats
    #: そのクラブの得失点差（自分 − 相手）
    club_margin: Floats
    total: Floats
    spectator_restricted: list[float | None]

    def __len__(self) -> int:
        return len(self.game_ids)

    @property
    def seasons(self) -> list[str]:
        seen: list[str] = []
        for season in self.season_ids:
            if season not in seen:
                seen.append(season)
        return seen

    def features(self, target: str) -> pd.DataFrame:
        """案Cの5列。**列順は `feature_keys()` が正**（実行ごとに変わらない）。"""
        return self.rows.loc[:, list(team_rate.feature_keys(target))]

    def target(self, name: str) -> Floats:
        """目的変数。カウントはその試合の値、成功率は `成功数 ÷ 試投数`。"""
        if name not in team_rate.TARGETS:
            raise MatrixError(f"目的変数が14項目にない: {name}")
        return np.asarray(self.actual[name], dtype=np.float64)

    def weights(self, name: str) -> Floats | None:
        """成功率3項目は試投数を重みにする（詳細設計 4.5）。カウントは None。"""
        for pct, _, attempt in team_rate.PCT_TARGETS:
            if pct == name:
                return np.asarray(self.attempts[attempt], dtype=np.float64)
        return None

    def as_training_data(self, target: str) -> TrainingData:
        """`walk_forward` に渡す形。**分割器を2つ作らない**（`evaluate.py`）。

        `club_win` / `club_margin` はクラブ視点の実績であり、**作り物ではない**。
        `walk_forward(target=...)` を使うため学習には使われないが、埋めておけば
        分割の条件（シーズンの出現順と件数）が game 視点とまったく同じに揃う。
        """
        return TrainingData(
            features=self.features(target),
            home_win=self.club_win,
            margin=self.club_margin,
            total=self.total,
            game_ids=self.game_ids,
            season_ids=self.season_ids,
            game_dates=self.game_dates,
            spectator_restricted=self.spectator_restricted,
        )


def _number(value: object) -> float | None:
    """数値として読めなければ None。**欠損を 0 と区別する**（規約5）。"""
    if value is None:
        return None
    try:
        number = float(str(value))
    except ValueError:
        return None
    return None if math.isnan(number) else number


def _pct_label(
    stats: dict[str, object], made: str, attempt: str,
) -> tuple[float, float]:
    """`(成功率, 試投数)`。試投0なら率は NaN（0除算を 0.0 と書かない）。"""
    attempts = _number(stats.get(attempt))
    makes = _number(stats.get(made))
    if attempts is None or makes is None or attempts <= 0:
        return float("nan"), 0.0
    return makes / attempts, attempts


def build_team_rate_matrix(ds: Dataset) -> TeamRateData:
    """終了した試合すべてについて、クラブ視点の行を2本ずつ組む。

    **自分のスタッツ行が無い試合は落とす。** 目的変数が作れない（`possessions` が
    値域外で NULL の試合や、取り込みがスタッツまで届いていない試合が実在する。
    詳細設計 1.3 / 4.8）。**欠けた目的変数を0で埋めない。**
    """
    games = ds.table("games")
    finished = games[games["status"] == "FINISHED"].copy()
    if finished.empty:
        raise MatrixError("終了した試合が1件もない")
    finished = finished.sort_values(["tipoff_at", "id"], kind="stable")

    # **`itertuples()` を使わない。** 戻り値の型が名前を持たないため、
    # `_asdict()` が mypy で列の dtype の union に解決されてしまう
    stats = ds.table("team_game_stats")
    by_key: dict[tuple[str, str], dict[str, object]] = {
        (str(record["game_id"]), str(record["club_id"])):
            {str(k): v for k, v in record.items()}
        for record in stats.to_dict("records")
    }

    prepared = prepare(ds)
    rows: list[dict[str, float]] = []
    actual: list[dict[str, float]] = []
    attempts: list[dict[str, float]] = []
    game_ids: list[str] = []
    club_ids: list[str] = []
    season_ids: list[str] = []
    game_dates: list[str] = []
    club_win: list[float] = []
    club_margin: list[float] = []
    total: list[float] = []
    restricted: list[float | None] = []

    for game in finished.itertuples():
        if pd.isna(game.home_score) or pd.isna(game.away_score):
            continue
        home = float(str(game.home_score))
        away = float(str(game.away_score))
        as_of = _as_of(game.tipoff_at)
        sides = (
            (str(game.home_club_id), home, away),
            (str(game.away_club_id), away, home),
        )
        for club_id, own_score, other_score in sides:
            stat = by_key.get((str(game.id), club_id))
            if stat is None:
                continue
            row = team_rate.build_team_rate_features(
                str(game.id), as_of=as_of, ds=ds, club_id=club_id,
                prepared=prepared)
            if row is None:
                continue
            labels: dict[str, float] = {}
            weight: dict[str, float] = {}
            usable = True
            for name in team_rate.COUNT_TARGETS:
                value = _number(stat.get(name))
                if value is None:
                    usable = False
                    break
                labels[name] = value
            if not usable:
                continue
            for pct, made, attempt in team_rate.PCT_TARGETS:
                labels[pct], weight[attempt] = _pct_label(stat, made, attempt)
            rows.append(row)
            actual.append(labels)
            attempts.append(weight)
            game_ids.append(str(game.id))
            club_ids.append(club_id)
            season_ids.append(str(game.season_id))
            game_dates.append(str(game.game_date))
            club_win.append(1.0 if own_score > other_score else 0.0)
            club_margin.append(own_score - other_score)
            total.append(own_score + other_score)
            value = getattr(game, "spectator_restricted", None)
            restricted.append(None if pd.isna(value) else float(value))

    if not rows:
        raise MatrixError("TeamRate の学習行を1件も作れなかった")
    return TeamRateData(
        rows=pd.DataFrame(rows, columns=list(team_rate.all_feature_keys())),
        actual=pd.DataFrame(actual, columns=list(team_rate.TARGETS)),
        attempts=pd.DataFrame(
            attempts, columns=[a for _, _, a in team_rate.PCT_TARGETS]),
        game_ids=game_ids,
        club_ids=club_ids,
        season_ids=season_ids,
        game_dates=game_dates,
        club_win=np.asarray(club_win, dtype=np.float64),
        club_margin=np.asarray(club_margin, dtype=np.float64),
        total=np.asarray(total, dtype=np.float64),
        spectator_restricted=restricted,
    )
