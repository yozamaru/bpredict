"""Elo のパラメータを探索する（工程8の段1。詳細設計 2.5）。

    python3 -m batch.jobs.tune_elo [--json out.json] [--top 10]

**段を分ける理由。** Elo を変えると `team_ratings` が変わり、それを読む特徴量も
変わるため、組み合わせごとに特徴量の再生成が必要になる。1回43分かかるので
300通りでは 215時間になる。段1は**特徴量を作らず**、Elo だけを入力にした
ロジスティック回帰の walk-forward Brier を目的関数にする。

**段1で Elo単体を目的関数にしてよい理由。** 探しているのは「Elo が実力差を
どれだけ正しく表すか」であり、それは Elo だけを入力にしたときの Brier で測れる。
他の特徴量を混ぜると、Elo の欠点を他の列が補ってしまい、Elo のパラメータの
良否が見えなくなる。

**段1の結果を段2で再探索しない。** 同じデータで2回選ぶと、選択の自由度が
二重に効いて過学習する。

**入力はスナップショットだけである**（絶対ルール3）。D1 を読まず、書かない。
"""

from __future__ import annotations

import argparse
import bisect
import itertools
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from numpy.typing import NDArray

from batch.features.dataset import Dataset, load_snapshot
from batch.model.dataset import TrainingData
from batch.model.evaluate import walk_forward
from batch.model.params import MAX_FOLDS
from batch.ratings.elo import DEFAULT_PARAMS, EloParams, recompute
from batch.ratings.params import (
    HOME_ADVANTAGE_GRID,
    K_GRID,
    LEAGUE_MEAN,
    PROMOTED_ELO_GRID,
    SEASON_REGRESSION_GRID,
)

type Floats = NDArray[np.float64]

DEFAULT_SNAPSHOT = Path("batch/snapshot")


class TuneError(RuntimeError):
    """探索を組めない入力。"""


class EloIndex:
    """`as_of_date < 対象試合日` の最新の Elo を速く引く。

    **`batch/features/team_strength.py` の `elo()` と同じ意味である。** あちらは
    呼び出しごとに `team_ratings` の全行を走査するため、12,540回の参照で
    1億5千万行の比較になる（特徴量生成が43分かかる主因）。探索では300通りを
    回すので、同じ意味を保ったまま二分探索にする。

    **一致はテストで固定する**（`test_job_tune_elo.py`）。意味がずれたら、
    探索で選んだパラメータが本番で別の値を出すことになる。
    """

    def __init__(self, ratings: pd.DataFrame) -> None:
        self._dates: dict[str, list[str]] = {}
        self._elos: dict[str, list[float]] = {}
        if ratings.empty:
            return
        ordered = ratings.sort_values(["club_id", "as_of_date"], kind="stable")
        for club_id, group in ordered.groupby("club_id", sort=False):
            self._dates[str(club_id)] = [str(v) for v in group["as_of_date"]]
            self._elos[str(club_id)] = [float(v) for v in group["elo"]]

    def at(self, club_id: str, game_date: str) -> float | None:
        """`as_of_date < game_date` の最新行。無ければ None（0埋めしない）。

        **不等号は `<` である。** `team_ratings` の1行はその試合日の終了時点の
        値なので、`<=` にすると当日の結果が混入する（詳細設計 1.4）。
        """
        dates = self._dates.get(club_id)
        if dates is None:
            return None
        position = bisect.bisect_left(dates, game_date)
        if position == 0:
            return None
        return self._elos[club_id][position - 1]


@dataclass(frozen=True)
class Trial:
    params: EloParams
    brier: float
    accuracy: float
    n: int
    #: Elo の行が無く 1500 で埋めた試合の数（シーズン最初の試合など）
    defaulted: int

    def as_dict(self) -> dict[str, object]:
        return {
            "k": self.params.k,
            "home_advantage": self.params.home_advantage,
            "season_regression": self.params.season_regression,
            "promoted_elo": self.params.promoted_elo,
            "brier": self.brier,
            "accuracy": self.accuracy,
            "n": self.n,
            "defaulted": self.defaulted,
        }


def finished_games(ds: Dataset) -> pd.DataFrame:
    """スコアの揃った終了試合を時系列に並べる（`build_matrix` と同じ条件）。"""
    games = ds.table("games")
    picked = games[games["status"] == "FINISHED"].copy()
    picked = picked[picked["home_score"].notna() & picked["away_score"].notna()]
    if picked.empty:
        raise TuneError("スコアの揃った終了試合が1件もない")
    return picked.sort_values(["tipoff_at", "id"], kind="stable")


def elo_only_matrix(
    games: pd.DataFrame, ratings: pd.DataFrame, *, league_mean: float = LEAGUE_MEAN,
) -> tuple[TrainingData, int]:
    """`elo_diff` 1列だけの学習行列と、既定値で埋めた件数を返す。"""
    index = EloIndex(ratings)
    diffs: list[float] = []
    home_win: list[float] = []
    season_ids: list[str] = []
    game_ids: list[str] = []
    dates: list[str] = []
    defaulted = 0

    for game in games.itertuples():
        date = str(game.game_date)
        home = index.at(str(game.home_club_id), date)
        away = index.at(str(game.away_club_id), date)
        if home is None or away is None:
            defaulted += 1
        # **欠損の既定値はリーグ平均**（詳細設計 2.2 の `elo_home` / `elo_away`）
        diffs.append((home if home is not None else league_mean)
                     - (away if away is not None else league_mean))
        home_win.append(1.0 if float(str(game.home_score)) > float(str(game.away_score)) else 0.0)
        season_ids.append(str(game.season_id))
        game_ids.append(str(game.id))
        dates.append(date)

    zeros = np.zeros(len(diffs), dtype=np.float64)
    data = TrainingData(
        features=pd.DataFrame({"elo_diff": diffs}),
        home_win=np.asarray(home_win, dtype=np.float64),
        margin=zeros, total=zeros,
        game_ids=game_ids, season_ids=season_ids, game_dates=dates,
        spectator_restricted=[None] * len(diffs),
    )
    return data, defaulted


def grid() -> list[EloParams]:
    """詳細設計 2.5 のグリッド。**実装で値を決めない**（`params.py` が出どころ）。"""
    return [
        EloParams(k=k, home_advantage=ha, season_regression=sr, promoted_elo=pe)
        for k, ha, sr, pe in itertools.product(
            K_GRID, HOME_ADVANTAGE_GRID, SEASON_REGRESSION_GRID, PROMOTED_ELO_GRID,
        )
    ]


def evaluate(
    games: pd.DataFrame, seasons: pd.DataFrame, params: EloParams, *,
    max_folds: int = MAX_FOLDS,
) -> Trial:
    """1組を評価する。**Elo を全期間 replay してから**行列を組む。"""
    from batch.jobs.train import logistic_learner

    ratings = recompute(games, seasons, params=params)
    data, defaulted = elo_only_matrix(games, ratings, league_mean=params.league_mean)
    result = walk_forward(data, logistic_learner(("elo_diff",)), max_folds=max_folds)
    return Trial(
        params=params, brier=result.brier, accuracy=result.accuracy,
        n=result.n, defaulted=defaulted,
    )


def search(
    ds: Dataset, *, max_folds: int = MAX_FOLDS,
    log: Callable[[str], None] = print,
) -> list[Trial]:
    """グリッド全体を評価し、Brier の昇順で返す。"""
    games = finished_games(ds)
    seasons = ds.table("seasons")
    combos = grid()
    log(f"tune_elo: {len(combos)} 通りを評価する（試合 {len(games):,}）")
    started = time.monotonic()
    trials = [evaluate(games, seasons, params, max_folds=max_folds) for params in combos]
    log(f"tune_elo: 評価し終えた（{time.monotonic() - started:.0f}秒）")
    return sorted(trials, key=lambda t: t.brier)


def render(trials: list[Trial], *, top: int = 10) -> str:
    best = trials[0]
    initial = next(
        (t for t in trials if t.params == DEFAULT_PARAMS),
        None,
    )
    lines = [
        f"評価 n={best.n:,}  組み合わせ {len(trials)}",
        "",
        f"{'K':>5}{'HA':>6}{'回帰':>7}{'昇格':>7}{'Brier':>9}{'Accuracy':>10}",
    ]
    for trial in trials[:top]:
        p = trial.params
        lines.append(
            f"{p.k:>5.0f}{p.home_advantage:>6.0f}{p.season_regression:>7.2f}"
            f"{p.promoted_elo:>7.0f}{trial.brier:>9.4f}{trial.accuracy:>10.4f}",
        )
    if initial is not None:
        gain = initial.brier - best.brier
        lines += [
            "",
            (f"初期値（K={initial.params.k:.0f} / HA={initial.params.home_advantage:.0f} / "
            f"回帰={initial.params.season_regression:.2f} / "
            f"昇格={initial.params.promoted_elo:.0f}）: Brier {initial.brier:.4f}"
            ),
            f"最良との差: {gain:+.4f}",
        ]
    lines.append(f"Elo の行が無く既定値で埋めた試合: {best.defaulted}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Elo のパラメータを探索する（段1）")
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--max-folds", type=int, default=MAX_FOLDS)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--json", type=Path, help="全組み合わせを書き出す先")
    args = parser.parse_args(argv)

    try:
        trials = search(load_snapshot(args.snapshot), max_folds=args.max_folds)
    except TuneError as error:
        print(f"tune_elo: 失敗（{type(error).__name__}: {error}）", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 — 公開ログの境界で本文を除去する
        print(f"tune_elo: 失敗（{type(error).__name__}）", file=sys.stderr)
        return 1

    print(render(trials, top=args.top))
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps([t.as_dict() for t in trials], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"tune_elo: {args.json} に書き出した")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
