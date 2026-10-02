"""勝敗モデルの学習と評価（工程8。詳細設計 4.5 / 4.6）。

    python -m batch.jobs.train                      評価して報告する
    python -m batch.jobs.train --json out.json      数値をファイルにも残す
    python -m batch.jobs.train --refresh            特徴量のキャッシュを作り直す

**入力はスナップショットだけである**（CLAUDE.md 絶対ルール3）。D1 を読まない。
**このジョブはモデルを登録しない** — 登録は `POST /internal/models` で、D1 の
書き込み枠を使う。評価と登録を分けることで、採用判定を何度でもやり直せる。

**特徴量をキャッシュする。** 6,270試合の特徴量生成に20分かかる（基本設計 2.5 の
実測から外挿）。**キャッシュの鍵はスナップショットの MANIFEST のハッシュ**であり、
スナップショットが変わればキャッシュは使われない（古い行列で評価する事故を防ぐ）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from numpy.typing import NDArray

from batch.features.builder import FEATURE_KEYS
from batch.features.dataset import Dataset, load_snapshot
from batch.model.baselines import fit_logistic, home_always
from batch.model.criteria import Decision, Inputs, passes_criteria
from batch.model.dataset import (
    TrainingData,
    build_matrix,
    constant_columns,
    null_rates,
    time_decay_weights,
)
from batch.model.evaluate import Evaluation, Learner, walk_forward
from batch.model.metrics import Difference, brier_difference, ece_noise_floor
from batch.model.params import ECE_FLOOR_K, MAX_FOLDS, TIME_DECAY_LAMBDA_INITIAL
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
    full: Evaluation
    ece_floor: float | None
    #: Elo単体との Brier 差（ベースラインとの差であり、現行モデルとの差ではない）
    difference: Difference
    null_rates: dict[str, float]
    constant_columns: list[str]
    decision: Decision
    #: 列ごとの gain の割合（fold ごとに合計100へ正規化した平均）。要件 6.2 が
    #: 「寄与度（SHAP / gain）と欠損率を測定する」と定める寄与度にあたる
    gain_share: dict[str, float] = field(default_factory=dict)
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
                summarize("winner(lightgbm)", self.winner),
                summarize("baseline1(home_always)", self.home),
                summarize("baseline2(elo_only_logistic)", self.elo),
                summarize("baseline3(all_feature_logistic)", self.full),
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
            "adopt": self.decision.adopt,
            "failures": self.decision.failures,
            "decision_notes": self.decision.notes,
        }


def evaluate_all(
    data: TrainingData, *, max_folds: int = MAX_FOLDS,
    decay: float = TIME_DECAY_LAMBDA_INITIAL,
    log: Callable[[str], None] = print,
) -> Report:
    """LightGBM と3段のベースラインを**同一の分割**で評価する。

    **fold をまとめてから測る**（`Evaluation.brier`）。fold ごとの指標を平均すると
    件数の違う fold が同じ重みになる。
    """
    weights = time_decay_weights(data.season_ids, data.seasons, lam=decay)
    gains: list[dict[str, float]] = []
    runs: dict[str, Evaluation] = {}
    for name, learner in (
        ("home_always", home_always_learner()),
        ("elo_only", logistic_learner(ELO_ONLY)),
        ("all_feature", logistic_learner()),
        ("winner", winner_learner(gains)),
    ):
        started = time.monotonic()
        runs[name] = walk_forward(data, learner, weights=weights, max_folds=max_folds)
        log(f"train: {name} を評価した（{time.monotonic() - started:.1f}秒）")

    winner = runs["winner"]
    floor = ece_noise_floor(winner.probs)
    difference = brier_difference(runs["elo_only"].probs, winner.probs, winner.actual)
    nulls = null_rates(data.features)
    constants = constant_columns(data.features)
    decision = passes_criteria(Inputs(
        n=winner.n,
        brier=winner.brier,
        baseline_elo_brier=runs["elo_only"].brier,
        null_rates=nulls,
        constant_columns=constants,
        ece=winner.ece,
        ece_floor=floor,
        # **初回登録である。** 現行モデルがないため Brier の比較と有意性の検査は
        # 課せない（詳細設計 4.6）。`difference` はベースラインとの差であり、
        # 現行モデルとの差ではないためゲートには渡さない
        current_brier=None,
        difference=None,
    ))
    return Report(
        n=winner.n,
        seasons=data.seasons,
        test_seasons=[f.test_season for f in winner.folds],
        winner=winner, home=runs["home_always"],
        elo=runs["elo_only"], full=runs["all_feature"],
        ece_floor=floor, difference=difference,
        null_rates=nulls, constant_columns=constants,
        gain_share=gain_share(gains), decision=decision,
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
        ("LightGBM（経路A: Winner）", report.winner),
        ("1 ホーム必勝", report.home),
        ("2 Elo差単体ロジスティック", report.elo),
        ("3 全特徴ロジスティック", report.full),
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="勝敗モデルを評価する（登録はしない）")
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--refresh", action="store_true", help="特徴量を作り直す")
    parser.add_argument("--max-folds", type=int, default=MAX_FOLDS)
    parser.add_argument("--json", type=Path, help="数値を書き出す先")
    args = parser.parse_args(argv)

    try:
        data = training_data(
            snapshot=args.snapshot, cache=args.cache, refresh=args.refresh,
        )
        report = evaluate_all(data, max_folds=args.max_folds)
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
