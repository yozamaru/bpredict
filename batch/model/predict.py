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
from collections.abc import Callable
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


# ---------------------------------------------------------------------------
# 30本（TEAM_RATE 14 / PLAYER_AVAIL / PLAYER_MIN / PLAYER_RATE 14）
# ---------------------------------------------------------------------------

#: 揃っていなければ個人スタッツを出さない30本（詳細設計 4.5.1）。
#: **勝敗の3本とは扱いが違う** — あちらが欠けたら1件も書かないが、こちらが
#: 欠けたら勝敗だけを書く（個人スタッツは「出ない」状態が設計上許されている。4.2）
RATE_TYPES = ("TEAM_RATE", "PLAYER_AVAIL", "PLAYER_MIN", "PLAYER_RATE")


@dataclass(frozen=True)
class RateModels:
    """30本。`versions` は `prediction_model_bundle` に書く。"""

    #: 目的変数 → booster（14本）
    team_rate: dict[str, Any]
    avail: Any
    minutes: Any
    #: 目的変数 → booster（14本）
    player_rate: dict[str, Any]
    #: 成功率3項目のシュリンクの `k`（`model_versions.params` から読む）
    shrink_k: dict[str, float]
    #: `(model_type, target)` → version
    versions: dict[tuple[str, str], str]


def load_active_rates(
    api: Any, *, league: str = "PREMIER",
    log: Callable[[str], None] = lambda _: None,
) -> RateModels | None:
    """30本を読み出す。**揃っていなければ `None` を返す**（4.2）。

    **例外にしない。** 勝敗の3本と違い、30本が欠けても勝率と予想スコアは出せる。
    **ただし黙って通さない** — 欠けている `model_type` をログに出す（登録し忘れに
    気づけるようにする）。
    """
    from batch.features.player_rate import AVAIL_KEYS, MINUTES_KEYS, rate_model_keys
    from batch.features.team_rate import PCT_TARGETS, TARGETS, feature_keys

    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for model in active_models(api, league):
        model_type = str(model.get("modelType") or "")
        if model_type in RATE_TYPES:
            rows[(model_type, str(model.get("target") or ""))] = model

    wanted: list[tuple[str, str, tuple[str, ...]]] = [
        ("PLAYER_AVAIL", "", tuple(AVAIL_KEYS)),
        ("PLAYER_MIN", "", tuple(MINUTES_KEYS)),
    ]
    for target in TARGETS:
        wanted.append(("TEAM_RATE", target, feature_keys(target)))
        wanted.append(("PLAYER_RATE", target, rate_model_keys(target)))

    absent = [f"{t}/{g}" if g else t for t, g, _ in wanted if (t, g) not in rows]
    if absent:
        log(f"predict: 30本が揃っていない（{len(absent)}本）: {' / '.join(absent[:6])}")
        return None

    pct_names = {name for name, _, _ in PCT_TARGETS}
    boosters: dict[tuple[str, str], Any] = {}
    shrink_k: dict[str, float] = {}
    try:
        for model_type, target, columns in wanted:
            row = rows[(model_type, target)]
            recorded = row.get("featureList")
            if not isinstance(recorded, str) or json.loads(recorded) != list(columns):
                raise PredictError(
                    f"{model_type}/{target or '-'} の feature_list が一致しない")
            version = str(row["version"])
            booster = _booster(
                fetch_artifact(api, version, expected_sha256=_sha(row)))
            names = list(booster.feature_name())
            if names and names != list(columns):
                raise PredictError(
                    f"{model_type}/{target or '-'} の列名が一致しない")
            boosters[(model_type, target)] = booster
            if model_type == "PLAYER_RATE" and target in pct_names:
                shrink_k[target] = _shrink_k_of(row, target)
    except RegistryError as error:
        raise PredictError(f"artifact を読めない（{error}）") from error

    return RateModels(
        team_rate={t: boosters[("TEAM_RATE", t)] for t in TARGETS},
        avail=boosters[("PLAYER_AVAIL", "")],
        minutes=boosters[("PLAYER_MIN", "")],
        player_rate={t: boosters[("PLAYER_RATE", t)] for t in TARGETS},
        shrink_k=shrink_k,
        versions={key: str(rows[key]["version"]) for key in rows},
    )


def _shrink_k_of(row: dict[str, Any], target: str) -> float:
    """`model_versions.params` の `shrink_k`。

    **学習時の値を読む。** `train_player.SHRINK_K` を推論側で参照すると、
    登録済みのモデルと定数が食い違ったときに**目的変数の定義が静かにずれる**
    （シュリンクは目的変数そのものを決める値である。2.3.1）。
    """
    params = row.get("params")
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except ValueError as error:
            raise PredictError(f"{target} の params を読めない") from error
    if not isinstance(params, dict) or "shrink_k" not in params:
        raise PredictError(f"{target} の params に shrink_k がない")
    value = float(params["shrink_k"])
    if not math.isfinite(value) or value <= 0:
        raise PredictError(f"{target} の shrink_k が正の有限値でない")
    return value
