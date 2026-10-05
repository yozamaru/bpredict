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

from batch.features import player_rate, team_rate
from batch.features.base import build_context
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


def as_of(value: object) -> datetime:
    """`games.tipoff_at` を `as_of` として読む。

    **解釈を1か所にする。** 推論（`batch/jobs/daily_ingest.py`）も学習と同じ
    関数を通る — 2か所で別に書くと、片方だけが素のタイムゾーンなしを許すような
    食い違いが静かに入る。
    """
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
            str(game.id), as_of=as_of(game.tipoff_at), ds=ds, prepared=prepared))
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
        moment = as_of(game.tipoff_at)
        sides = (
            (str(game.home_club_id), home, away),
            (str(game.away_club_id), away, home),
        )
        for club_id, own_score, other_score in sides:
            stat = by_key.get((str(game.id), club_id))
            if stat is None:
                continue
            row = team_rate.build_team_rate_features(
                str(game.id), as_of=moment, ds=ds, club_id=club_id,
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


# --- PlayerMinutes（第2段）の学習行列（詳細設計 2.3.1） ---


@dataclass(frozen=True)
class PlayerMinutesData:
    """**1行は「1試合 × 出場した選手」。**

    `player_game_stats` に行があることが「出場した」の定義であり、この表が
    そのまま行の集合になる（2.3.1）。第1段（出場するか）と第3段（14項目）は
    設計にない仕様を要するため、ここでは扱わない。
    """

    features: pd.DataFrame
    #: その試合の出場時間（分）
    minutes: Floats
    game_ids: list[str]
    player_ids: list[str]
    club_ids: list[str]
    season_ids: list[str]
    game_dates: list[str]

    def __len__(self) -> int:
        return len(self.game_ids)

    @property
    def seasons(self) -> list[str]:
        seen: list[str] = []
        for season in self.season_ids:
            if season not in seen:
                seen.append(season)
        return seen

    def as_training_data(self) -> TrainingData:
        """`walk_forward` に渡す形。**分割器を2つ作らない**（`evaluate.py`）。

        `home_win` / `margin` / `total` はこの行に意味を持たないため NaN を置く。
        `walk_forward(target=...)` に出場時間を渡すため学習には使われず、
        **回帰の結果に分類の指標を呼ぶと `Evaluation._require_binary` が落とす**。
        """
        blank = np.full(len(self), np.nan, dtype=np.float64)
        return TrainingData(
            features=self.features,
            home_win=blank,
            margin=blank,
            total=blank,
            game_ids=self.game_ids,
            season_ids=self.season_ids,
            game_dates=self.game_dates,
            spectator_restricted=[None] * len(self),
        )


@dataclass(frozen=True)
class PlayerAvailData:
    """**1行は「1試合 × 出場しうる選手」**（2.3.1）。

    候補は `player_rate.candidates()` が過去の出場実績から作る。**ロスター
    （`player_seasons`）を使わない** — 取得した断面は時点を持たず、季中の加入が
    加入前の試合の候補に現れる（絶対ルール1）。

    正例は「`player_game_stats` にその試合・そのクラブの行がある」ことである。
    **`minutes` が NULL でも正例にする** — ボックススコアに名前があれば出場で
    あり、第2段が NULL 行を落とすのは目的変数が出場時間だからである。
    """

    features: pd.DataFrame
    #: 出場したか（0/1）
    played: Floats
    game_ids: list[str]
    player_ids: list[str]
    club_ids: list[str]
    season_ids: list[str]
    game_dates: list[str]
    #: **候補に入らなかった出場の件数。** 季の1試合目の新加入が主である
    #: （2.3.1 の実測で全体の 1.3%）。**黙って落とさず数える**
    uncovered: int = 0
    #: 候補が空だった（試合, クラブ）の件数。前季の実績がないクラブ
    empty_candidates: int = 0

    def __len__(self) -> int:
        return len(self.game_ids)

    @property
    def seasons(self) -> list[str]:
        seen: list[str] = []
        for season in self.season_ids:
            if season not in seen:
                seen.append(season)
        return seen

    def as_training_data(self) -> TrainingData:
        """`walk_forward` に渡す形。**分割器を2つ作らない**（`evaluate.py`）。

        目的変数は 0/1 なので `home_win` に載せる。`margin` / `total` は
        この行に意味を持たないため NaN を置く。
        """
        blank = np.full(len(self), np.nan, dtype=np.float64)
        return TrainingData(
            features=self.features,
            home_win=self.played,
            margin=blank,
            total=blank,
            game_ids=self.game_ids,
            season_ids=self.season_ids,
            game_dates=self.game_dates,
            spectator_restricted=[None] * len(self),
        )


def build_player_avail_matrix(ds: Dataset) -> PlayerAvailData:
    """候補 × 終了した試合の行を組む。

    **候補に入らなかった出場を正例として足さない。** 足すと全列が既定値の行が
    正例になり、「履歴がない → 出場する」を教えることになる（既定値は最も
    出場しない層のためのものである。2.3.1）。**件数は `uncovered` に残す。**
    """
    games = ds.table("games")
    finished = games[games["status"] == "FINISHED"].copy()
    if finished.empty:
        raise MatrixError("終了した試合が1件もない")
    finished = finished.sort_values(["tipoff_at", "id"], kind="stable")

    stats = ds.table("player_game_stats")
    appeared: dict[tuple[str, str], set[str]] = {}
    for record in stats.to_dict("records"):
        key = (str(record["game_id"]), str(record["club_id"]))
        appeared.setdefault(key, set()).add(str(record["player_id"]))

    prepared = prepare(ds)
    rows: list[dict[str, float]] = []
    played: list[float] = []
    game_ids: list[str] = []
    player_ids: list[str] = []
    club_ids: list[str] = []
    season_ids: list[str] = []
    game_dates: list[str] = []
    uncovered = 0
    empty = 0

    for game in finished.itertuples():
        game_id = str(game.id)
        # **`Context` は試合ごとに1回だけ作る**（`player_rate.minutes_row`）
        context = build_context(game_id, as_of(game.tipoff_at), ds, prepared)
        for club_id in (str(game.home_club_id), str(game.away_club_id)):
            names = player_rate.candidates(context, club_id)
            actual = appeared.get((game_id, club_id), set())
            if not names:
                empty += 1
                uncovered += len(actual)
                continue
            uncovered += len(actual - set(names))
            for player_id in names:
                rows.append(player_rate.avail_row(context, club_id, player_id))
                played.append(1.0 if player_id in actual else 0.0)
                game_ids.append(game_id)
                player_ids.append(player_id)
                club_ids.append(club_id)
                season_ids.append(str(game.season_id))
                game_dates.append(str(game.game_date))

    if not rows:
        raise MatrixError("PlayerAvail の学習行を1件も作れなかった")
    return PlayerAvailData(
        features=pd.DataFrame(rows, columns=list(player_rate.AVAIL_KEYS)),
        played=np.asarray(played, dtype=np.float64),
        game_ids=game_ids,
        player_ids=player_ids,
        club_ids=club_ids,
        season_ids=season_ids,
        game_dates=game_dates,
        uncovered=uncovered,
        empty_candidates=empty,
    )


def build_player_minutes_matrix(ds: Dataset) -> PlayerMinutesData:
    """出場した選手の行を、終了した試合すべてについて組む。

    **出場時間が欠けている行は落とす。** 0 として扱うと「出場したが0分」になり、
    目的変数が壊れる（`player_game_stats.minutes` は NULL を取りうる）。
    """
    games = ds.table("games")
    finished = games[games["status"] == "FINISHED"].copy()
    if finished.empty:
        raise MatrixError("終了した試合が1件もない")
    finished = finished.sort_values(["tipoff_at", "id"], kind="stable")

    stats = ds.table("player_game_stats")
    by_game: dict[str, list[dict[str, object]]] = {}
    for record in stats.to_dict("records"):
        by_game.setdefault(str(record["game_id"]), []).append(
            {str(k): v for k, v in record.items()})

    prepared = prepare(ds)
    rows: list[dict[str, float]] = []
    minutes: list[float] = []
    game_ids: list[str] = []
    player_ids: list[str] = []
    club_ids: list[str] = []
    season_ids: list[str] = []
    game_dates: list[str] = []

    for game in finished.itertuples():
        appearances = by_game.get(str(game.id))
        if not appearances:
            continue
        # **`Context` は試合ごとに1回だけ作る。** 行ごとに作ると記憶が毎行
        # 捨てられ、1試合16人で16倍の無駄になる（`player_rate.minutes_row`）
        context = build_context(
            str(game.id), as_of(game.tipoff_at), ds, prepared)
        for stat in appearances:
            played = _number(stat.get("minutes"))
            if played is None:
                continue
            club_id = str(stat["club_id"])
            player_id = str(stat["player_id"])
            row = player_rate.minutes_row(context, club_id, player_id)
            if row is None:
                continue
            rows.append(row)
            minutes.append(played)
            game_ids.append(str(game.id))
            player_ids.append(player_id)
            club_ids.append(club_id)
            season_ids.append(str(game.season_id))
            game_dates.append(str(game.game_date))

    if not rows:
        raise MatrixError("PlayerMinutes の学習行を1件も作れなかった")
    return PlayerMinutesData(
        features=pd.DataFrame(rows, columns=list(player_rate.MINUTES_KEYS)),
        minutes=np.asarray(minutes, dtype=np.float64),
        game_ids=game_ids,
        player_ids=player_ids,
        club_ids=club_ids,
        season_ids=season_ids,
        game_dates=game_dates,
    )


@dataclass(frozen=True)
class PlayerRateData:
    """**1行は「1試合 × 出場した選手」**（2.3.1）。第2段と同じ行の集合である。

    14項目（カウント11 + 成功率3）を**1枚の行列で持つ**。モデルごとに渡す列は
    `player_rate.rate_model_keys(target)` が選ぶ（2.3.1）。

    **目的変数は生の実績で持ち、変換は学習時に行う。**

    | 区分 | 行列が持つもの | 学習時の目的変数 |
    |---|---|---|
    | カウント11項目 | その試合のカウント | **`カウント ÷ 出場時間`**（per-minute） |
    | 成功率3項目 | その試合の成功数・試投数 | `(made + k × prior) / (att + k)`、重みは `att` |

    `k` は探索の対象であり（2.3.1）、**行列の段では当てない**。

    **`features["pred_minutes"]` は NaN である。** 第2段の出力であり、fold ごとに
    学習季だけで当てはめて埋める（2.3.1）。そのために第2段の列
    （`minutes_features`）を同じ行の順序で併せ持つ。
    """

    features: pd.DataFrame
    #: 第2段の列（`player_rate.MINUTES_KEYS`）。`pred_minutes` を埋めるために持つ
    minutes_features: pd.DataFrame
    #: その試合の実際の出場時間（分）。**per-minute の分母**
    minutes: Floats
    #: カウント11項目の実績（列名は `player_rate.COUNT_TARGETS`）
    counts: pd.DataFrame
    #: 成功率3項目の実績。列は `{pct}_made` / `{pct}_att`
    shots: pd.DataFrame
    game_ids: list[str]
    player_ids: list[str]
    club_ids: list[str]
    season_ids: list[str]
    game_dates: list[str]

    def __len__(self) -> int:
        return len(self.game_ids)

    @property
    def seasons(self) -> list[str]:
        seen: list[str] = []
        for season in self.season_ids:
            if season not in seen:
                seen.append(season)
        return seen

    def subset(self, keep: Floats) -> PlayerRateData:
        """行を絞る。**索引の値を振り直さない。**

        `walk_forward` は `features[mask]` でブール選択するため、学習関数には
        **元の索引の値を持った DataFrame** が渡る。第2段を fold ごとに当てはめる
        には、そこから `minutes_features` と `minutes` を引き当てる必要がある
        （2.3.1）。索引を振り直すと、この対応が静かに崩れる。
        """
        mask = np.asarray(keep, dtype=bool)
        if mask.size != len(self):
            raise MatrixError("絞り込みの件数が学習行列と合わない")
        if not mask.any():
            raise MatrixError("絞り込みで行が1件も残らなかった")
        picked = [i for i, taken in enumerate(mask) if taken]
        return PlayerRateData(
            features=self.features[mask],
            minutes_features=self.minutes_features[mask],
            minutes=self.minutes[mask],
            counts=self.counts[mask],
            shots=self.shots[mask],
            game_ids=[self.game_ids[i] for i in picked],
            player_ids=[self.player_ids[i] for i in picked],
            club_ids=[self.club_ids[i] for i in picked],
            season_ids=[self.season_ids[i] for i in picked],
            game_dates=[self.game_dates[i] for i in picked],
        )

    def as_training_data(self, features: pd.DataFrame) -> TrainingData:
        """`walk_forward` に渡す形。**分割器を2つ作らない**（`evaluate.py`）。

        目的変数は `walk_forward(target=...)` で渡すため、ここでは NaN を置く。
        **`features` を引数に取る** — モデルごとに列が違い、`pred_minutes` も
        fold ごとに埋まるため、`self.features` をそのまま渡せない。
        """
        if len(features) != len(self):
            raise MatrixError("特徴量の行数が一致しない")
        blank = np.full(len(self), np.nan, dtype=np.float64)
        return TrainingData(
            features=features,
            home_win=blank,
            margin=blank,
            total=blank,
            game_ids=self.game_ids,
            season_ids=self.season_ids,
            game_dates=self.game_dates,
            spectator_restricted=[None] * len(self),
        )


def build_player_rate_matrix(ds: Dataset) -> PlayerRateData:
    """出場した選手の行を、終了した試合すべてについて組む。

    **出場時間が欠けている行、0分の行は落とす。** per-minute の分母になるため、
    0 を入れるとゼロ除算になり、既定値で埋めると目的変数が壊れる。

    **`Context` は試合ごとに1回だけ作る**（`player_rate.minutes_row` と同じ）。
    """
    games = ds.table("games")
    finished = games[games["status"] == "FINISHED"].copy()
    if finished.empty:
        raise MatrixError("終了した試合が1件もない")
    finished = finished.sort_values(["tipoff_at", "id"], kind="stable")

    stats = ds.table("player_game_stats")
    by_game: dict[str, list[dict[str, object]]] = {}
    for record in stats.to_dict("records"):
        by_game.setdefault(str(record["game_id"]), []).append(
            {str(k): v for k, v in record.items()})

    prepared = prepare(ds)
    rows: list[dict[str, float]] = []
    minutes_rows: list[dict[str, float]] = []
    minutes: list[float] = []
    counts: list[dict[str, float]] = []
    shots: list[dict[str, float]] = []
    game_ids: list[str] = []
    player_ids: list[str] = []
    club_ids: list[str] = []
    season_ids: list[str] = []
    game_dates: list[str] = []

    for game in finished.itertuples():
        appearances = by_game.get(str(game.id))
        if not appearances:
            continue
        context = build_context(
            str(game.id), as_of(game.tipoff_at), ds, prepared)
        for stat in appearances:
            played = _number(stat.get("minutes"))
            if played is None or played <= 0:
                continue
            club_id = str(stat["club_id"])
            player_id = str(stat["player_id"])
            if club_id not in (context.home_club_id, context.away_club_id):
                continue
            stage_two = player_rate.minutes_row(context, club_id, player_id)
            if stage_two is None:
                continue
            row = player_rate.rate_row(context, club_id, player_id)
            if row is None:
                continue

            count_values = {
                target: _number(stat.get(target))
                for target in team_rate.COUNT_TARGETS
            }
            shot_values: dict[str, float] = {}
            for name, made, attempt in team_rate.PCT_TARGETS:
                shot_values[f"{name}_made"] = _or_nan(_number(stat.get(made)))
                shot_values[f"{name}_att"] = _or_nan(_number(stat.get(attempt)))

            rows.append(row)
            minutes_rows.append(stage_two)
            minutes.append(played)
            counts.append({k: _or_nan(v) for k, v in count_values.items()})
            shots.append(shot_values)
            game_ids.append(str(game.id))
            player_ids.append(player_id)
            club_ids.append(club_id)
            season_ids.append(str(game.season_id))
            game_dates.append(str(game.game_date))

    if not rows:
        raise MatrixError("PlayerRate の学習行を1件も作れなかった")
    return PlayerRateData(
        features=pd.DataFrame(
            rows, columns=list(player_rate.all_rate_matrix_keys())),
        minutes_features=pd.DataFrame(
            minutes_rows, columns=list(player_rate.MINUTES_KEYS)),
        minutes=np.asarray(minutes, dtype=np.float64),
        counts=pd.DataFrame(counts, columns=list(team_rate.COUNT_TARGETS)),
        shots=pd.DataFrame(
            shots,
            columns=[f"{name}_{part}" for name, _, _ in team_rate.PCT_TARGETS
                     for part in ("made", "att")]),
        game_ids=game_ids,
        player_ids=player_ids,
        club_ids=club_ids,
        season_ids=season_ids,
        game_dates=game_dates,
    )


def _or_nan(value: float | None) -> float:
    """**実績の欠損は NaN で持つ。** 0 で埋めると「記録なし」が「0回」になる。"""
    return float("nan") if value is None else float(value)
