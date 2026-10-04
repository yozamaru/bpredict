"""推論（`batch/model/predict.py`。詳細設計 4.2 のステップ4）。

**HTTP にも D1 にも触らない。** `/internal/*` の応答を模した辞書を返す偽物を渡す。
LightGBM は TOTAL の artifact を読むところだけで必要になるため、そこだけ差し替える。
"""
from __future__ import annotations

import json
from typing import Any

import numpy as np
import pytest

from batch.model import predict as mod
from batch.model.baselines import Logistic
from batch.model.predict import PredictError, load_active
from batch.model.registry import artifact_sha256, dump_logistic

COLUMNS = ["elo_diff", "rest_days_diff"]


def logistic_artifact() -> str:
    return dump_logistic(
        Logistic(intercept=0.1, coefficients=np.array([0.01, 0.05])), COLUMNS)


class FakeApi:
    """`active_models` と `fetch_artifact` が呼ぶ `get` だけを持つ。"""

    def __init__(self, *, rows: list[dict[str, Any]] | None = None,
                 artifacts: dict[str, str] | None = None) -> None:
        self.rows = rows if rows is not None else default_rows()
        self.artifacts = artifacts or {
            "winner-v1.0.0": logistic_artifact(),
            "total-v1.0.0": "tree\n",
        }
        self.asked: list[str] = []

    def get(self, path: str, query: dict[str, str] | None = None) -> object:
        self.asked.append(path)
        if path == "models/active":
            return {"models": self.rows}
        version = path.split("/")[1]
        text = self.artifacts[version]
        return {"version": version, "artifactText": text,
                "artifactSha256": artifact_sha256(text)}


def default_rows() -> list[dict[str, Any]]:
    features = json.dumps(COLUMNS)
    return [
        {"version": "winner-v1.0.0", "modelType": "WINNER", "featureList": features,
         "winProbSource": "WINNER", "marginSigma": None,
         "artifactSha256": artifact_sha256(logistic_artifact())},
        {"version": "margin-v1.0.0", "modelType": "MARGIN", "featureList": features,
         "marginSigma": 12.92, "artifactSha256": None},
        {"version": "total-v1.0.0", "modelType": "TOTAL", "featureList": features,
         "marginSigma": None, "artifactSha256": artifact_sha256("tree\n")},
    ]


class FakeBooster:
    """`total.predict(DataFrame)` と `feature_name()` だけを持つ。"""

    def __init__(self, value: float = 162.0, names: list[str] | None = None) -> None:
        self.value = value
        self.names = names if names is not None else COLUMNS

    def feature_name(self) -> list[str]:
        return self.names

    def predict(self, frame: Any) -> np.ndarray:
        return np.full(len(frame), self.value)


@pytest.fixture(autouse=True)
def no_lightgbm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "_booster", lambda _text: FakeBooster())


# --- 3本が揃っていること ---

def test_all_three_models_are_required() -> None:
    """**揃わなければ1件も予測しない**（詳細設計 4.2）。

    勝率だけ出して予想スコアを NULL にする経路を作らない — 画面は両方を
    前提にしている（要件 F-02 / F-03）。
    """
    for missing in ("WINNER", "MARGIN", "TOTAL"):
        rows = [r for r in default_rows() if r["modelType"] != missing]
        with pytest.raises(PredictError, match="揃っていない"):
            load_active(FakeApi(rows=rows), COLUMNS)


def test_the_margin_artifact_is_not_fetched() -> None:
    """**MARGIN の本体は取らない。** 使うのは σ だけである（要件 6.1.1）。"""
    api = FakeApi()
    load_active(api, COLUMNS)
    assert "models/margin-v1.0.0/artifact" not in api.asked
    assert "models/winner-v1.0.0/artifact" in api.asked
    assert "models/total-v1.0.0/artifact" in api.asked


def test_the_margin_row_carries_the_sigma() -> None:
    """σ は `MARGIN` の行から読む（詳細設計 4.5.1）。"""
    models = load_active(FakeApi(), COLUMNS)
    assert models.margin_sigma == pytest.approx(12.92)
    assert set(models.versions) == {"WINNER", "MARGIN", "TOTAL"}


def test_a_missing_sigma_is_rejected() -> None:
    """σ が無い・非正なら推論しない（`Φ⁻¹` の係数が決まらない）。"""
    for bad in (None, 0, -1.0):
        rows = default_rows()
        rows[1]["marginSigma"] = bad
        with pytest.raises(PredictError, match="margin_sigma"):
            load_active(FakeApi(rows=rows), COLUMNS)


# --- 列の照合 ---

def test_a_different_feature_list_is_rejected() -> None:
    """**列がずれたまま推論が通るのが最も悪い壊れ方である。**

    特徴量を1つ足したあとに古い artifact を読むと、係数と列の対応が崩れる。
    """
    rows = default_rows()
    rows[0]["featureList"] = json.dumps(["elo_diff"])
    with pytest.raises(PredictError, match="feature_list"):
        load_active(FakeApi(rows=rows), COLUMNS)


def test_the_column_order_matters() -> None:
    """並びが違うだけでも落とす（係数は位置で効く）。"""
    with pytest.raises(PredictError, match="feature_list"):
        load_active(FakeApi(), list(reversed(COLUMNS)))


def test_the_total_booster_names_are_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    """LightGBM 側も列名を照合する（ロジスティックと同じ理由）。"""
    monkeypatch.setattr(
        mod, "_booster", lambda _t: FakeBooster(names=["elo_diff", "other"]))
    with pytest.raises(PredictError, match="列名"):
        load_active(FakeApi(), COLUMNS)


# --- 1試合の予測 ---

def test_the_prediction_is_internally_consistent() -> None:
    """勝率と予想スコアが同じ値から出ること（要件 6.1.1 / A-11）。"""
    models = load_active(FakeApi(), COLUMNS)
    out = models.predict({"elo_diff": 80.0, "rest_days_diff": 1.0})
    assert 0.05 <= out.home_win_prob <= 0.95
    assert out.home_score + out.away_score == pytest.approx(out.total)
    assert out.home_score - out.away_score == pytest.approx(out.margin)
    # 勝率が 0.5 より大きいならホームの予想得点が上
    assert (out.home_win_prob > 0.5) == (out.home_score > out.away_score)


def test_the_probability_is_clamped() -> None:
    """外挿域の確率を `[0.05, 0.95]` に収める（詳細設計 6.4）。"""
    models = load_active(FakeApi(), COLUMNS)
    extreme = models.predict({"elo_diff": 100_000.0, "rest_days_diff": 0.0})
    assert extreme.home_win_prob == pytest.approx(0.95)
    # クランプが効いていれば得点差も有限である
    assert abs(extreme.margin) < 100.0


def test_a_missing_feature_is_rejected() -> None:
    """特徴量が足りなければ落とす。**0 で埋めない。**"""
    models = load_active(FakeApi(), COLUMNS)
    with pytest.raises(PredictError, match="足りない"):
        models.predict({"elo_diff": 10.0})


def test_a_non_finite_feature_is_rejected() -> None:
    """NaN を通すと勝率が NaN になり、`Φ⁻¹` が落ちる前に静かに壊れる。"""
    models = load_active(FakeApi(), COLUMNS)
    with pytest.raises(PredictError, match="有限でない"):
        models.predict({"elo_diff": float("nan"), "rest_days_diff": 0.0})


def test_the_features_are_read_in_the_recorded_order() -> None:
    """**辞書の並びに頼らない。** 渡す順を変えても同じ予測になること。"""
    models = load_active(FakeApi(), COLUMNS)
    straight = models.predict({"elo_diff": 80.0, "rest_days_diff": 1.0})
    shuffled = models.predict({"rest_days_diff": 1.0, "elo_diff": 80.0})
    assert straight == shuffled
