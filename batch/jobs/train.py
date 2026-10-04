"""勝敗モデルの学習と評価（工程8。詳細設計 4.5 / 4.6）。

**本番の勝敗モデルは全特徴ロジスティック回帰である**（要件 6.1.1）。LightGBM は
ベースライン3段目に降りた。両方を同じ `walk_forward` で評価し、**採用する経路は
要件 6.1 の「判定の順序」を実装した `adopt_route` が決める**（ここで決め打ちしない）。

    python -m batch.jobs.train                      評価して報告する
    python -m batch.jobs.train --json out.json      数値をファイルにも残す
    python -m batch.jobs.train --refresh            特徴量のキャッシュを作り直す
    python -m batch.jobs.train --register           採用判定を通ったら登録する

**入力はスナップショットだけである**（CLAUDE.md 絶対ルール3）。D1 を読まない。
**既定では登録しない。** 登録は `POST /internal/models` で D1 の書き込み枠を使う
ため、`--register` を付けたときだけ行う（詳細設計 4.5.1）。評価と登録を分けると、
採用判定を何度でもやり直せる。

**`--register` は採用判定を通らなければ何も送らない。** 基準未達で登録だけして
おく経路は作らない — `model_versions` に有効でない行が溜まると、どれが本番かを
`is_active` 以外で判断する余地が生まれる。

**特徴量をキャッシュする。** 6,270試合の特徴量生成に20分かかる（基本設計 2.5 の
実測から外挿）。**キャッシュの鍵はスナップショットの MANIFEST のハッシュ**であり、
スナップショットが変わればキャッシュは使われない（古い行列で評価する事故を防ぐ）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from numpy.typing import NDArray

from batch.features.builder import FEATURE_KEYS
from batch.features.dataset import Dataset, load_snapshot
from batch.loader.api import InternalApi, LoaderError
from batch.model.baselines import Logistic, fit_logistic, home_always
from batch.model.criteria import Decision, Inputs, passes_criteria
from batch.model.dataset import (
    TrainingData,
    build_matrix,
    constant_columns,
    null_rates,
    time_decay_weights,
)
from batch.model.evaluate import Evaluation, Learner, walk_forward
from batch.model.final import DEFAULT_SEMVER, Evaluations, build_records
from batch.model.metrics import Difference, brier_difference, ece_noise_floor
from batch.model.params import (
    ECE_FLOOR_K,
    MAX_FOLDS,
    MIN_EFFECT,
    TIME_DECAY_LAMBDA_INITIAL,
)
from batch.model.registry import (
    RegistryError,
    fetch_artifact,
    load_logistic,
    register,
)
from batch.model.train_score import (
    learn_score,
    margin_from_win_prob,
    win_prob_from_margin,
)
from batch.model.train_winner import learn_winner

type Floats = NDArray[np.float64]

DEFAULT_SNAPSHOT = Path("batch/snapshot")
DEFAULT_CACHE = Path("batch/cache/training-matrix.parquet")

#: ベースライン2「Elo差単体のロジスティック回帰」が使う列（要件 6.4）。
#: **`elo_home` / `elo_away` を入れない** — 要件は「Elo差単体」と定めている
ELO_ONLY = ("elo_diff",)


class TrainError(RuntimeError):
    """評価を組めない。"""


def logistic_learner(columns: Sequence[str] | None = None) -> Learner:
    """ロジスティック回帰の `Learner`。`columns` が None なら全特徴を使う。

    **検証セットは使わない。** early stopping がないため受け取って捨てる
    （`Learner` の契約を揃えるためだけに引数に取る）。
    """
    def learn(
        train_x: pd.DataFrame, train_y: Floats, train_w: Floats,
        _valid_x: pd.DataFrame, _valid_y: Floats,
    ) -> tuple[Callable[[pd.DataFrame], Floats], int]:
        picked = list(columns) if columns is not None else list(train_x.columns)
        model = fit_logistic(train_x[picked].to_numpy(dtype=np.float64), train_y, weights=train_w)

        def predict(features: pd.DataFrame) -> Floats:
            return model.predict(features[picked].to_numpy(dtype=np.float64))

        return predict, 0

    return learn


def winner_learner(collected: list[dict[str, float]]) -> Learner:
    """`learn_winner` に gain の集計を足した `Learner`。

    **fold ごとの gain を集めて平均する。** 1 fold の gain は学習データの量が違う
    ため直接は比べられないが、**各 fold で列の合計を 100 に正規化**してから平均すれば
    「その fold でモデルが何に依存したか」の割合として読める。
    """
    def learn(
        train_x: pd.DataFrame, train_y: Floats, train_w: Floats,
        valid_x: pd.DataFrame, valid_y: Floats,
    ) -> tuple[Callable[[pd.DataFrame], Floats], int]:
        return learn_winner(
            train_x, train_y, train_w, valid_x, valid_y,
            on_gain=collected.append,
        )

    return learn


def gain_share(collected: list[dict[str, float]]) -> dict[str, float]:
    """fold ごとに合計100へ正規化した gain の平均（百分率）。

    **合計が0の fold は飛ばす。** 木が1本も育たなかった fold を 0 として平均に
    入れると、分母だけが増えて全列の割合が薄まる。
    """
    shares: dict[str, list[float]] = {}
    used = 0
    for fold in collected:
        total = sum(fold.values())
        if total <= 0:
            continue
        used += 1
        for name, value in fold.items():
            shares.setdefault(name, []).append(100.0 * value / total)
    if used == 0:
        return {}
    return {name: sum(v) / used for name, v in shares.items()}


def home_always_learner() -> Learner:
    """ベースライン1「ホームが必ず勝つ」。学習しない。"""
    def learn(
        _train_x: pd.DataFrame, _train_y: Floats, _train_w: Floats,
        _valid_x: pd.DataFrame, _valid_y: Floats,
    ) -> tuple[Callable[[pd.DataFrame], Floats], int]:
        def predict(features: pd.DataFrame) -> Floats:
            return home_always(len(features))

        return predict, 0

    return learn


# --- 特徴量のキャッシュ ---

def manifest_digest(snapshot: Path) -> str:
    """スナップショットの MANIFEST のハッシュ。キャッシュの鍵の片方になる。"""
    path = snapshot / "MANIFEST.json"
    if not path.exists():
        raise TrainError("MANIFEST.json がない")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def feature_digest() -> str:
    """特徴量のキー一覧のハッシュ。**キャッシュの鍵のもう片方**。

    **スナップショットだけを鍵にすると、特徴量を増やしたときに古い行列が読まれる。**
    特徴量を1つ足してもスナップショットは変わらないため digest が一致し、
    **増やす前の特徴量で評価した結果が「増やした後の結果」として出てしまう。**
    しかも落ちないため気づけない（2026-10-02 に工程8の続きへ入る前に判明）。

    順序も鍵に含める。`load_cached` は列名ではなく位置で特徴量を取り出すため、
    キーの並びが変わっただけでも作り直す必要がある。
    """
    return hashlib.sha256("\n".join(FEATURE_KEYS).encode("utf-8")).hexdigest()


def load_cached(cache: Path, digest: str) -> TrainingData | None:
    """鍵が一致したときだけ使う。**不一致なら黙って捨てる**（作り直す）。"""
    side = cache.with_suffix(".json")
    if not cache.exists() or not side.exists():
        return None
    meta = json.loads(side.read_text(encoding="utf-8"))
    if meta.get("manifest_sha256") != digest:
        return None
    if meta.get("feature_sha256") != feature_digest():
        return None
    frame = pd.read_parquet(cache)
    targets = ("home_win", "margin", "total")
    keys = ("game_id", "season_id", "game_date", "spectator_restricted")
    features = frame.drop(columns=[*targets, *keys])
    return TrainingData(
        features=features,
        home_win=frame["home_win"].to_numpy(dtype=np.float64),
        margin=frame["margin"].to_numpy(dtype=np.float64),
        total=frame["total"].to_numpy(dtype=np.float64),
        game_ids=[str(v) for v in frame["game_id"]],
        season_ids=[str(v) for v in frame["season_id"]],
        game_dates=[str(v) for v in frame["game_date"]],
        spectator_restricted=[
            None if pd.isna(v) else float(v) for v in frame["spectator_restricted"]
        ],
    )


def save_cache(cache: Path, digest: str, data: TrainingData) -> None:
    frame = data.features.copy()
    frame["home_win"] = data.home_win
    frame["margin"] = data.margin
    frame["total"] = data.total
    frame["game_id"] = data.game_ids
    frame["season_id"] = data.season_ids
    frame["game_date"] = data.game_dates
    frame["spectator_restricted"] = data.spectator_restricted
    cache.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(cache, index=False)
    cache.with_suffix(".json").write_text(
        json.dumps(
            {
                "manifest_sha256": digest,
                "feature_sha256": feature_digest(),
                "feature_keys": list(FEATURE_KEYS),
                "rows": len(data),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def training_data(
    *, snapshot: Path = DEFAULT_SNAPSHOT, cache: Path = DEFAULT_CACHE,
    refresh: bool = False, log: Callable[[str], None] = print,
) -> TrainingData:
    digest = manifest_digest(snapshot)
    if not refresh:
        cached = load_cached(cache, digest)
        if cached is not None:
            log(f"train: 特徴量はキャッシュを使う（{len(cached):,} 行）")
            return cached
    log("train: 特徴量を作る（キャッシュが無効か未作成）")
    started = time.monotonic()
    ds: Dataset = load_snapshot(snapshot)
    data = build_matrix(ds)
    log(f"train: 特徴量を作った（{len(data):,} 行 / {time.monotonic() - started:.0f}秒）")
    save_cache(cache, digest, data)
    return data


# --- 評価 ---

@dataclass(frozen=True)
class Report:
    n: int
    seasons: list[str]
    test_seasons: list[str]
    winner: Evaluation
    home: Evaluation
    elo: Evaluation
    lightgbm: Evaluation
    ece_floor: float | None
    #: Elo単体との Brier 差（ベースラインとの差であり、現行モデルとの差ではない）
    difference: Difference
    null_rates: dict[str, float]
    constant_columns: list[str]
    decision: Decision
    #: 列ごとの gain の割合（fold ごとに合計100へ正規化した平均）。要件 6.2 が
    #: 「寄与度（SHAP / gain）と欠損率を測定する」と定める寄与度にあたる
    gain_share: dict[str, float] = field(default_factory=dict)
    #: 得点差・合計得点の評価（工程12）。**同一の分割で測る**
    margin: Evaluation | None = None
    total: Evaluation | None = None
    #: 経路B（`Φ(margin / σ)`）の評価と、実測した σ（P0-11）
    route_b: Evaluation | None = None
    margin_sigma: float | None = None
    #: **採用する経路**の Elo単体に対する Brier 差。A-09 の判定はこれで行う
    adopted_route: str = "A"
    adopted_difference: Difference | None = None
    #: `adopt_route` が通った枝（要件 6.1 の判定の順序）
    route_notes: list[str] = field(default_factory=list)
    #: **勝率から導いた**得点差で測ったチーム得点 MAE（要件 6.1.1）
    derived_score_mae: float | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        def summarize(name: str, ev: Evaluation) -> dict[str, object]:
            return {
                "name": name, "n": ev.n, "brier": ev.brier,
                "accuracy": ev.accuracy, "log_loss": ev.log_loss, "ece": ev.ece,
            }

        return {
            "n": self.n,
            "seasons": self.seasons,
            "test_seasons": self.test_seasons,
            "models": [
                summarize("winner(all_feature_logistic)", self.winner),
                summarize("baseline1(home_always)", self.home),
                summarize("baseline2(elo_only_logistic)", self.elo),
                summarize("baseline3(lightgbm)", self.lightgbm),
            ],
            "ece_floor_q95": self.ece_floor,
            "brier_difference_vs_elo": {
                "point": self.difference.point,
                "ci_low": self.difference.ci_low,
                "ci_high": self.difference.ci_high,
                "significant": self.difference.significant,
            },
            "constant_columns": self.constant_columns,
            "worst_null_rate": max(self.null_rates.values(), default=0.0),
            # **何で測ったかを残す。** 次に特徴量を足したときに比べる相手になる
            "feature_keys": list(FEATURE_KEYS),
            "gain_share": self.gain_share,
            "score": None if self.margin is None or self.total is None else {
                "margin_mae": self.margin.mae,
                "total_mae": self.total.mae,
                "margin_sigma": self.margin_sigma,
                "team_score_mae": team_score_mae(self.margin, self.total),
            },
            "adopted_route": self.adopted_route,
            "route_notes": self.route_notes,
            "derived_team_score_mae": self.derived_score_mae,
            "adopted_difference": None if self.adopted_difference is None else {
                "point": self.adopted_difference.point,
                "ci_low": self.adopted_difference.ci_low,
                "ci_high": self.adopted_difference.ci_high,
                "significant": self.adopted_difference.significant,
            },
            "route_b": None if self.route_b is None else {
                "brier": self.route_b.brier,
                "accuracy": self.route_b.accuracy,
                "ece": self.route_b.ece,
            },
            "adopt": self.decision.adopt,
            "failures": self.decision.failures,
            "decision_notes": self.decision.notes,
        }


def team_score_mae(margin: Evaluation, total: Evaluation) -> float:
    """**チーム得点**の MAE。要件 6.4 と付録B が言う「予想スコアの MAE」。

    `home = (total + margin) / 2` なので、ホームの誤差は
    `((total の誤差) + (margin の誤差)) / 2` になる。アウェイは margin の符号が
    反転するだけで、**両チームをまとめた MAE は同じ式の平均で出る。**

    **得点差の MAE（10.27点）と混同しない。** 付録B の見立て「8〜10点」は
    チーム得点についてのものである。
    """
    if not margin.folds or not total.folds:
        return float("nan")
    dm = margin.probs - margin.actual
    dt = total.probs - total.actual
    if dm.shape != dt.shape:
        return float("nan")
    home = np.abs((dt + dm) / 2.0)
    away = np.abs((dt - dm) / 2.0)
    return float(np.concatenate([home, away]).mean())


def meets_a09(difference: Difference) -> bool:
    """受け入れ基準 A-09 を満たすか（要件 12章 / 詳細設計 4.6）。

    **「有意」だけでは足りない。** 点推定の差が最小実質差（0.003）以上あることも
    要求する。逆に「0.003 以上」だけでも足りない（ノイズで届きうる）。
    """
    return difference.significant and difference.point >= MIN_EFFECT


def adopt_route(
    a: tuple[Evaluation, Difference], b: tuple[Evaluation, Difference],
) -> tuple[str, list[str]]:
    """採用する経路を決める（要件 6.1「判定の順序」をそのまま実装する）。

    1. **A-09 を満たす経路だけを候補にする。** 品質ゲート（要件12章）は A-09 を
       絶対条件にしており、これを満たさない経路は選べない
    2. 候補が2つ残ったら — B が A より 0.003 以上悪ければ A、差が 0.003 未満なら
       整合性を優先して B
    3. 候補が1つなら、それを採る

    **1段目を実装に持つ。** 旧版のコードは2段目だけを書いており、A-09 を満たさない
    経路を規則が強制しうる状態だった（2026-10-04 に実際に衝突した）。**結論を
    定数で埋めない** — 列や σ が変われば候補の集合も変わる。

    候補が0件なら `"NONE"` を返す。呼び出し側は採用判定を落とす（`passes_criteria`
    がベースライン比較で落とすため、ここで例外を投げる必要はない）。
    """
    (a_eval, a_diff), (b_eval, b_diff) = a, b
    notes: list[str] = []
    candidates = [
        name for name, diff in (("A", a_diff), ("B", b_diff)) if meets_a09(diff)
    ]
    notes.append(f"A-09 を満たす経路: {' / '.join(candidates) or 'なし'}")
    if not candidates:
        notes.append("候補が0件のため経路を採用しない（要件 6.1 の1段目）")
        return "NONE", notes
    if len(candidates) == 1:
        notes.append(f"候補が1つのため経路{candidates[0]}を採る（3段目）")
        return candidates[0], notes
    gap = b_eval.brier - a_eval.brier
    route = "A" if gap >= MIN_EFFECT else "B"
    notes.append(
        f"候補が2つ。B − A = {gap:+.4f} のため経路{route}を採る（2段目）",
    )
    return route, notes


def derived_team_score_mae(
    winner: Evaluation, margin: Evaluation, total: Evaluation, sigma: float,
) -> float:
    """**勝率から導いた**得点差で測るチーム得点 MAE（要件 6.1.1）。

    本番の予想スコアは `margin = σ · Φ⁻¹(p)` から作る。`team_score_mae` が測るのは
    Margin 回帰の出力を使った旧経路であり、**両方を出して比べる**（実測では
    8.832 対 8.839 で、勝率から導いた方がわずかに良い）。

    fold の並びが3つで一致していることを確かめる。**一致していなければ落とす** —
    別のシーズンの予測と実績を突き合わせた MAE は、小さく出ても意味がない。
    """
    seasons = [[f.test_season for f in ev.folds] for ev in (winner, margin, total)]
    if not seasons[0] or len({tuple(s) for s in seasons}) != 1:
        raise TrainError("fold の並びが経路間で一致しない")
    home_err: list[Floats] = []
    away_err: list[Floats] = []
    for w, m, t in zip(winner.folds, margin.folds, total.folds, strict=True):
        derived = margin_from_win_prob(w.probs, sigma)
        dm = derived - m.actual
        dt = t.probs - t.actual
        home_err.append(np.abs((dt + dm) / 2.0))
        away_err.append(np.abs((dt - dm) / 2.0))
    return float(np.concatenate([*home_err, *away_err]).mean())


def _home_win_of(data: TrainingData, season: str) -> Floats:
    """あるシーズンの `home_win`。**経路B の実測値に使う。**

    `margin` の fold が持つ `actual` は得点差であって勝敗ではない。
    経路B の Brier は「勝ったか」に対して測る必要がある。
    """
    picked = np.asarray(data.season_ids) == season
    actual: Floats = data.home_win[picked]
    return actual


@dataclass(frozen=True)
class Current:
    """現行モデルを同じウィンドウで再評価した結果（詳細設計 4.6）。

    `evaluation` が None なら比較できなかった（理由は `reason`）。
    """

    version: str | None = None
    evaluation: Evaluation | None = None
    reason: str = ""


def frozen_learner(model: Logistic, columns: Sequence[str]) -> Learner:
    """**学習をしない `Learner`。** 渡された学習データを捨て、保存済みの係数で予測する。

    これを `walk_forward` に渡すことで、**ウィンドウが構造的に同一になる**
    （分割器を2つ作らない。`evaluate.py`）。

    **現行モデルは全データで当てはめてある**ため（4.5.1）、どの fold の test に
    対しても予測が in-sample になる。**比較は現行モデルに有利に偏る** — 差し替えが
    起きにくくなる方向であり、要件 6.5 が意図している向きである（詳細設計 4.6）。
    """
    picked = list(columns)

    def learn(
        _train_x: pd.DataFrame, _train_y: Floats, _train_w: Floats,
        _valid_x: pd.DataFrame, _valid_y: Floats,
    ) -> tuple[Callable[[pd.DataFrame], Floats], int]:
        def predict(features: pd.DataFrame) -> Floats:
            return model.predict(features[picked].to_numpy(dtype=np.float64))

        return predict, 0

    return learn


def current_evaluation(
    api: Any, data: TrainingData, *, weights: Floats, max_folds: int = MAX_FOLDS,
) -> Current:
    """現行モデルを**同じ walk-forward ウィンドウ**で再評価する（詳細設計 4.6）。

    **記録済みの `cv_brier` は使わない。** 学習当時のウィンドウの値であり、
    新旧で評価対象が違えば比較になっていない。
    """
    try:
        meta = api.get("metrics/active", {"modelType": "WINNER", "target": "",
                                          "league": "PREMIER"})
    except LoaderError as error:
        return Current(reason=f"現行モデルの照会に失敗した（{error}）")
    if not isinstance(meta, dict) or not isinstance(meta.get("version"), str):
        return Current(reason="現行モデルがない")

    version = str(meta["version"])
    try:
        artifact = fetch_artifact(api, version)
    except (LoaderError, RegistryError) as error:
        return Current(
            version=version, reason=f"現行モデルの artifact を読めない（{error}）")

    columns = list(data.features.columns)
    try:
        # **まず今の列で読む。** 一致すればそのまま比較できる
        model = load_logistic(artifact, columns)
        used = columns
    except RegistryError:
        recorded = _artifact_features(artifact)
        if recorded is None:
            return Current(version=version, reason="現行モデルの列が読めない")
        missing = [c for c in recorded if c not in columns]
        if missing:
            # **採用しない。** 比較できない以上、条件1〜2 を課せない（4.6）
            return Current(
                version=version,
                reason=(
                    f"現行モデルの列が今の行列に無い（{len(missing)}列）。"
                    "比較できないため採用しない。意図した差し替えなら --initial を使う"
                ),
            )
        model = load_logistic(artifact, recorded)
        used = recorded

    evaluation = walk_forward(
        data, frozen_learner(model, used), weights=weights, max_folds=max_folds)
    return Current(version=version, evaluation=evaluation)


def _artifact_features(artifact_text: str) -> list[str] | None:
    """artifact が記録している列の並び。読めなければ None。"""
    try:
        payload = json.loads(artifact_text)
    except json.JSONDecodeError:
        return None
    features = payload.get("features") if isinstance(payload, dict) else None
    if not isinstance(features, list) or not all(isinstance(f, str) for f in features):
        return None
    return [str(f) for f in features]


def _current_for(
    api: Any | None, data: TrainingData, *, weights: Floats, initial: bool,
    max_folds: int,
) -> Current:
    """比較する相手を決める（詳細設計 4.6）。

    **`--initial` を逃げ道として使う。** 「比較できないときは通す」という新しい
    分岐を作らない — それが v1.97 まで黙って起きていたことである。
    """
    if initial:
        return Current(reason="--initial が指定された（現行モデルと比較しない）")
    if api is None:
        # 登録しない実行では D1 に触らない（`API_BASE_URL` が無い手元でも回る）
        return Current(reason="登録しないため現行モデルと比較しない")
    return current_evaluation(api, data, weights=weights, max_folds=max_folds)


def evaluate_all(
    data: TrainingData, *, max_folds: int = MAX_FOLDS,
    decay: float = TIME_DECAY_LAMBDA_INITIAL,
    api: Any | None = None,
    initial: bool = False,
    log: Callable[[str], None] = print,
) -> Report:
    """LightGBM と3段のベースラインを**同一の分割**で評価する。

    **fold をまとめてから測る**（`Evaluation.brier`）。fold ごとの指標を平均すると
    件数の違う fold が同じ重みになる。
    """
    weights = time_decay_weights(data.season_ids, data.seasons, lam=decay)
    gains: list[dict[str, float]] = []
    runs: dict[str, Evaluation] = {}
    # **`winner` が本番モデル（全特徴ロジスティック回帰）である**（要件 6.1.1）。
    # LightGBM はベースライン3段目。名前と役を一致させておく — 旧版は `winner` が
    # LightGBM で、採用判定も gain もそちらを見ていた
    for name, learner in (
        ("home_always", home_always_learner()),
        ("elo_only", logistic_learner(ELO_ONLY)),
        ("winner", logistic_learner()),
        ("lightgbm", winner_learner(gains)),
    ):
        started = time.monotonic()
        runs[name] = walk_forward(data, learner, weights=weights, max_folds=max_folds)
        log(f"train: {name} を評価した（{time.monotonic() - started:.1f}秒）")

    # --- 得点差と合計得点（工程12）。**同じ `walk_forward` を通す** ---
    # P0-11 は「同一の walk-forward ウィンドウで Brier と ECE を測る」ことを
    # 要件としており（要件 6.1）、分割を共有しないと経路A と B の比較が成立しない
    margin = walk_forward(
        data, learn_score, target=data.margin, weights=weights, max_folds=max_folds)
    total = walk_forward(
        data, learn_score, target=data.total, weights=weights, max_folds=max_folds)
    log(f"train: margin / total を評価した（MAE {margin.mae:.2f} / {total.mae:.2f}）")

    # --- 経路B（`Φ(margin / σ)`）。σ は out-of-fold の残差から実測する ---
    sigma = margin.residual_sigma
    route_b = Evaluation(folds=tuple(
        replace(fold, probs=win_prob_from_margin(fold.probs, sigma),
                actual=_home_win_of(data, fold.test_season))
        for fold in margin.folds
    ))
    log(f"train: 経路B を評価した（σ {sigma:.2f} / Brier {route_b.brier:.4f}）")

    # **有意性は経路ごとに測り、採用は `adopt_route` が決める。** 要件 6.1 の
    # 判定の順序は3段あり、**1段目が「A-09 を満たす経路だけを候補にする」**で
    # ある。A に対して測った有意性を B の採用根拠にすると判定の対象がずれる
    winner = runs["winner"]
    elo = runs["elo_only"]
    difference = brier_difference(elo.probs, winner.probs, winner.actual)
    route_b_difference = brier_difference(elo.probs, route_b.probs, route_b.actual)
    adopted_route, route_notes = adopt_route(
        (winner, difference), (route_b, route_b_difference))
    adopted_difference = difference if adopted_route != "B" else route_b_difference
    log(f"train: 採用する経路は {adopted_route}（{' / '.join(route_notes)}）")

    floor = ece_noise_floor(winner.probs)
    nulls = null_rates(data.features)
    constants = constant_columns(data.features)
    # --- 現行モデルとの比較（詳細設計 4.6 の条件1〜2） ---
    # **`difference`（ベースラインとの差）をここに渡さない。** あれは A-09 の判定で
    # あり、現行モデルとの差ではない。v1.97 まで `current_brier=None` を固定で
    # 渡しており、**条件1〜2 が一度も課されていなかった**
    current = _current_for(api, data, weights=weights, initial=initial,
                           max_folds=max_folds)
    adopted_probs = winner.probs if adopted_route != "B" else route_b.probs
    adopted_actual = winner.actual if adopted_route != "B" else route_b.actual
    adopted_brier = winner.brier if adopted_route != "B" else route_b.brier
    current_brier: float | None = None
    current_difference: Difference | None = None
    if current is not None and current.evaluation is not None:
        # **採用する経路で測る**（基本設計 2.3）。A に対して測った差を B の採用根拠に
        # すると判定の対象がずれる
        current_brier = current.evaluation.brier
        current_difference = brier_difference(
            current.evaluation.probs, adopted_probs, adopted_actual)
        log(
            f"train: 現行 {current.version} を同じウィンドウで再評価した"
            f"（Brier {current_brier:.6f} / 差 {current_difference.point:+.6f}）"
        )
    elif current is not None and current.reason:
        log(f"train: 現行モデルと比較しない（{current.reason}）")

    decision = passes_criteria(Inputs(
        n=winner.n,
        brier=adopted_brier,
        baseline_elo_brier=elo.brier,
        null_rates=nulls,
        constant_columns=constants,
        ece=winner.ece,
        ece_floor=floor,
        current_brier=current_brier,
        difference=current_difference,
    ))
    return Report(
        n=winner.n,
        seasons=data.seasons,
        test_seasons=[f.test_season for f in winner.folds],
        winner=winner, home=runs["home_always"],
        elo=elo, lightgbm=runs["lightgbm"],
        ece_floor=floor, difference=difference,
        null_rates=nulls, constant_columns=constants,
        gain_share=gain_share(gains), decision=decision,
        margin=margin, total=total, route_b=route_b, margin_sigma=sigma,
        adopted_route=adopted_route, adopted_difference=adopted_difference,
        route_notes=route_notes,
        derived_score_mae=derived_team_score_mae(winner, margin, total, sigma),
    )


def render(report: Report) -> str:
    lines = [
        (f"評価 n={report.n:,}  シーズン {len(report.seasons)}  "
        f"テスト fold {len(report.test_seasons)}（{' / '.join(report.test_seasons)}）"
        ),
        "",
        f"{'モデル':<30}{'Brier':>9}{'Accuracy':>11}{'LogLoss':>10}{'ECE':>9}",
    ]
    for name, ev in (
        ("全特徴ロジスティック（本番）", report.winner),
        ("1 ホーム必勝", report.home),
        ("2 Elo差単体ロジスティック", report.elo),
        ("3 LightGBM", report.lightgbm),
    ):
        ece = "—" if ev.ece is None else f"{ev.ece:.4f}"
        lines.append(
            f"{name:<30}{ev.brier:>9.4f}{ev.accuracy:>11.4f}{ev.log_loss:>10.4f}{ece:>9}",
        )
    gain = report.elo.brier - report.winner.brier
    d = report.difference
    lines += [
        "",
        (f"Elo単体との Brier 差: {gain:+.4f}"
        f"（95%CI {getattr(d, 'ci_low', float('nan')):+.4f}, "
        f"{getattr(d, 'ci_high', float('nan')):+.4f} / "
        f"{'有意' if getattr(d, 'significant', False) else '有意でない'}）"
        ),
        "ECE ノイズフロア95%点: "
        + ("—" if report.ece_floor is None else f"{report.ece_floor:.4f}")
        + "  →  閾値 "
        + ("—" if report.ece_floor is None else f"{report.ece_floor * ECE_FLOOR_K:.4f}"),
        f"定数列: {' / '.join(report.constant_columns) or 'なし'}",
        f"欠損率の最大: {max(report.null_rates.values(), default=0.0):.1%}",
    ]
    if report.margin is not None and report.total is not None:
        # 要件 6.4 は予想スコアの指標を MAE と定める。付録B の見立ては 8〜10点
        lines += [
            "",
            "予想スコア（得点差と合計得点から導出。各チーム得点を独立に回帰しない）",
            (f"  得点差 MAE   {report.margin.mae:>7.2f}点"
             f"（残差 σ {report.margin.residual_sigma:.2f}）"),
            f"  合計得点 MAE {report.total.mae:>7.2f}点",
            (f"  チーム得点 MAE {team_score_mae(report.margin, report.total):>7.2f}点"
             "（**旧経路**。Margin 回帰の出力から）"),
        ]
        if report.derived_score_mae is not None:
            # 本番は勝率から導く（要件 6.1.1）。**両方出して比べる**
            lines.append(
                f"  **チーム得点 MAE {report.derived_score_mae:>5.2f}点**"
                "（本番。勝率から σ·Φ⁻¹(p) で導く。付録B の見立ては 8〜10点）",
            )
    if report.route_b is not None and report.margin_sigma is not None:
        a, b = report.winner, report.route_b
        lines += [
            "",
            "勝率の導出経路（P0-11。**同一の分割で測る**）",
            f"{'経路':<28}{'Brier':>9}{'Accuracy':>11}{'ECE':>9}",
            (f"{'A: Winner 直接':<28}{a.brier:>9.4f}{a.accuracy:>11.4f}"
             + ("—" if a.ece is None else f"{a.ece:>9.4f}")),
            (f"{'B: Φ(margin / σ)':<28}{b.brier:>9.4f}{b.accuracy:>11.4f}"
             + ("—" if b.ece is None else f"{b.ece:>9.4f}")),
            "",
            f"実測した σ: {report.margin_sigma:.2f}点（out-of-fold の残差）",
            f"**採用する経路: {report.adopted_route}**",
        ]
        lines += [f"  {note}" for note in report.route_notes]
        if report.adopted_difference is not None:
            d2 = report.adopted_difference
            lines.append(
                f"採用する経路（{report.adopted_route}）の Elo単体との差: "
                f"{d2.point:+.4f}（95%CI {d2.ci_low:+.4f}, {d2.ci_high:+.4f} / "
                f"{'有意' if d2.significant else '有意でない'}）  ← A-09 の判定",
            )
    if report.gain_share:
        # **全体の Brier だけでは1項目の採否を判断できない**（1項目の効果は
        # walk-forward の CI の幅より小さい。要件付録B）。モデルが実際にその列に
        # 依存したかを gain で見る（要件 6.2）
        lines += ["", "寄与度（gain の割合。fold ごとに合計100へ正規化した平均）"]
        ordered = sorted(report.gain_share.items(), key=lambda kv: -kv[1])
        for name, share in ordered:
            lines.append(f"  {name:<24}{share:>7.2f}%")
    lines += [
        "",
        f"採用判定: {report.decision.summary}",
    ]
    return "\n".join(lines)


def evaluations_of(report: Report) -> Evaluations:
    """`Report` から登録に必要なものだけを取り出す（依存の向きを `batch.model`
    から `batch.jobs` に向けないため）。"""
    if report.margin is None or report.total is None:
        raise TrainError("得点差・合計得点の評価がない（登録できない）")
    return Evaluations(
        winner=report.winner, home=report.home, elo=report.elo,
        margin=report.margin, total=report.total,
        ece_floor=report.ece_floor, null_rates=report.null_rates,
    )


def register_models(
    data: TrainingData, report: Report, *, api: InternalApi,
    semver: str = DEFAULT_SEMVER, log: Callable[[str], None] = print,
) -> list[str]:
    """採用判定を通ったときだけ3本を登録して有効化する（詳細設計 4.5.1）。

    **判定を通らなければ空を返す。** 現行モデルを継続する。
    """
    if not report.decision.adopt:
        log(f"train: 採用基準を満たさないため登録しない（{report.decision.summary}）")
        return []
    records = build_records(data, evaluations_of(report), semver=semver)
    registered: list[str] = []
    for record in records:
        size = register(api, record, activate=True)
        log(f"train: {record.version} を登録して有効化した（artifact {size:,} バイト）")
        registered.append(record.version)
    return registered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="勝敗モデルを評価する（登録はしない）")
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--refresh", action="store_true", help="特徴量を作り直す")
    parser.add_argument("--max-folds", type=int, default=MAX_FOLDS)
    parser.add_argument("--json", type=Path, help="数値を書き出す先")
    parser.add_argument(
        "--register", action="store_true",
        help="採用判定を通ったら WINNER / MARGIN / TOTAL を登録して有効化する")
    parser.add_argument("--model-version", default=DEFAULT_SEMVER,
                        help="版（既定 1.0.0。学習条件を変えたら上げる）")
    parser.add_argument(
        "--initial", action="store_true",
        help="現行モデルと比較しない（初回登録、または列を変えて比較できないとき）")
    parser.add_argument("--dry-run", action="store_true", help="D1 には書かない")
    args = parser.parse_args(argv)

    # **登録するときだけ内部APIを組む。** 現行モデルとの比較は D1 を要するため、
    # 評価だけの実行（`API_BASE_URL` が無い手元）でも回るようにする
    api = (
        InternalApi(
            os.environ.get("API_BASE_URL", ""),
            os.environ.get("INGEST_TOKEN", ""),
            dry_run=args.dry_run,
        )
        if args.register
        else None
    )
    try:
        data = training_data(
            snapshot=args.snapshot, cache=args.cache, refresh=args.refresh,
        )
        report = evaluate_all(
            data, max_folds=args.max_folds, api=api, initial=args.initial)
    except TrainError as error:
        print(f"train: 失敗（{type(error).__name__}: {error}）", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 — 公開ログの境界で本文を除去する
        print(f"train: 失敗（{type(error).__name__}）", file=sys.stderr)
        return 1

    print(render(report))
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(report.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8",
        )
        print(f"train: {args.json} に書き出した")

    if args.register and api is not None:
        try:
            versions = register_models(
                data, report, api=api, semver=args.model_version)
        except (TrainError, LoaderError) as error:
            # **自前の文言を出す。** これらの例外は URL も応答本文もトークンも
            # 含まない（基本設計 4.3）。型名だけにすると切り分けに再実行が要る
            print(f"train: 登録に失敗（{type(error).__name__}: {error}）",
                  file=sys.stderr)
            return 1
        except Exception as error:  # noqa: BLE001 — 本文に何が入るか保証できない
            print(f"train: 登録に失敗（{type(error).__name__}）", file=sys.stderr)
            return 1
        if not versions:
            return 1          # 採用基準未達は PARTIAL 相当。通知に乗せる
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
