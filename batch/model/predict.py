"""推論（詳細設計 4.2 のステップ4）。

**入力はスナップショットだけである**（絶対ルール3）。モデルの読み出しは
**運用上の読み取り**であり `/internal/*` の GET で行う（3.4）。

**3本が揃わなければ1件も予測しない**（WINNER / MARGIN / TOTAL）。勝率だけ出して
予想スコアを NULL にする経路を作らない — 画面は両方を前提にしている
（要件 F-02 / F-03）。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from batch.model.baselines import Logistic
from batch.model.explain import Explainer
from batch.model.registry import (
    RegistryError,
    active_models,
    fetch_artifact,
    load_logistic,
    load_logistic_means,
)
from batch.model.train_score import margin_from_win_prob, scores
from batch.model.train_winner import clamp_win_prob

#: 揃っていなければ推論しない3本（詳細設計 4.5.1）。
REQUIRED_TYPES = ("WINNER", "MARGIN", "TOTAL")


class PredictError(RuntimeError):
    """推論を組めない。**例外に本文を入れない**（絶対ルール4）。"""


@dataclass(frozen=True)
class Prediction:
    """1試合ぶんの予測。**勝率と予想スコアが同じ値から出る**（要件 6.1.1）。"""

    home_win_prob: float
    margin: float
    total: float
    home_score: float
    away_score: float


@dataclass(frozen=True)
class ActiveModels:
    """有効モデル一式。`versions` は `prediction_model_bundle` に書く。"""

    winner: Logistic
    total: Any
    margin_sigma: float
    feature_list: list[str]
    versions: dict[str, str]
    #: 根拠の寄与（詳細設計 2.7）。**推論ループの外で1回だけ作る。**
    explainer: Explainer

    def predict(self, features: dict[str, float]) -> Prediction:
        """特徴量1件から勝率・得点差・合計得点・両チーム得点を出す。

        **順序は `feature_list` に従う。** 辞書の並びに頼らない — 列がずれたまま
        推論が通るのが最も悪い壊れ方である（`load_logistic` も同じ照合をする）。
        """
        missing = [k for k in self.feature_list if k not in features]
        if missing:
            raise PredictError(f"特徴量が足りない: {len(missing)}列")
        row = pd.DataFrame(
            [[float(features[k]) for k in self.feature_list]], columns=self.feature_list)
        values = row.to_numpy(dtype=np.float64)
        if not np.isfinite(values).all():
            raise PredictError("特徴量に有限でない値がある")

        # **クランプを通す**（6.4）。外挿域の保険であり、`Φ⁻¹` の有限性の保証でもある
        prob = float(clamp_win_prob(self.winner.predict(values))[0])
        margin = float(margin_from_win_prob(np.array([prob]), self.margin_sigma)[0])
        total = float(np.asarray(self.total.predict(row), dtype=np.float64)[0])
        home, away = scores(np.array([margin]), np.array([total]))
        return Prediction(
            home_win_prob=prob, margin=margin, total=total,
            home_score=float(home[0]), away_score=float(away[0]),
        )


def _booster(artifact_text: str):  # type: ignore[no-untyped-def]
    """LightGBM を遅延インポートして artifact から戻す（`train_winner` と同じ理由）。"""
    import lightgbm

    return lightgbm.Booster(model_str=artifact_text)


def load_active(api: Any, feature_list: list[str], *, league: str = "PREMIER") -> ActiveModels:
    """有効モデルを読み出して組み立てる。

    **列の一覧を照合する。** `model_versions.feature_list` が呼び出し側と
    一致しなければ落とす — 特徴量を1つ足したあとに古い artifact を読むと、
    **列がずれたまま推論が通る**。
    """
    rows = {str(m.get("modelType")): m for m in active_models(api, league)}
    absent = [t for t in REQUIRED_TYPES if t not in rows]
    if absent:
        raise PredictError(f"有効モデルが揃っていない: {' / '.join(absent)}")

    for model_type in REQUIRED_TYPES:
        recorded = rows[model_type].get("featureList")
        if not isinstance(recorded, str):
            raise PredictError(f"{model_type} に feature_list がない")
        if json.loads(recorded) != feature_list:
            raise PredictError(f"{model_type} の feature_list が呼び出し側と一致しない")

    sigma = rows["MARGIN"].get("marginSigma")
    if not isinstance(sigma, (int, float)) or not math.isfinite(float(sigma)) or sigma <= 0:
        raise PredictError("MARGIN の margin_sigma が正の有限値でない")

    # **MARGIN の artifact は取らない。** 使うのは σ だけである（要件 6.1.1 —
    # 得点差は勝率から導くため、Margin 回帰の予測値そのものは本番では使わない）。
    # **それでも `modelBundle` には入れる** — σ がこのモデルから来ている
    versions = {t: str(rows[t]["version"]) for t in REQUIRED_TYPES}
    try:
        # **WINNER の artifact は1回しか取らない。** 係数と平均は同じ JSON にあり、
        # 2回取ると（同じ版でも）読んだ内容が一致する保証を別に要する
        winner_artifact = fetch_artifact(
            api, versions["WINNER"], expected_sha256=_sha(rows["WINNER"]))
        winner = load_logistic(winner_artifact, feature_list)
        means = load_logistic_means(winner_artifact, feature_list)
        total = _booster(
            fetch_artifact(api, versions["TOTAL"], expected_sha256=_sha(rows["TOTAL"])))
    except RegistryError as error:
        raise PredictError(f"artifact を読めない（{error}）") from error

    names = list(total.feature_name())
    if names and names != feature_list:
        raise PredictError("TOTAL の列名が呼び出し側と一致しない")
    return ActiveModels(
        winner=winner, total=total, margin_sigma=float(sigma),
        feature_list=list(feature_list), versions=versions,
        explainer=Explainer(
            features=tuple(feature_list),
            intercept=winner.intercept,
            coefficients=winner.coefficients,
            means=means,
        ),
    )


def _sha(row: dict[str, Any]) -> str | None:
    value = row.get("artifactSha256")
    return value if isinstance(value, str) and value else None
