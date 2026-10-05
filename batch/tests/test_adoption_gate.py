"""採用判定の現行モデル比較（詳細設計 4.6。v1.98 で実装した）。

**v1.97 まで条件1〜2 が一度も課されていなかった** — `train.py` が
`current_brier=None` を固定で渡しており、`winner-v1.0.0` が有効なのに
「現行モデルがない」と出ていた。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

from batch.jobs import train
from batch.loader.api import LoaderError
from batch.model.baselines import Logistic
from batch.model.dataset import TrainingData
from batch.model.evaluate import walk_forward
from batch.model.registry import dump_logistic

COLUMNS = ["elo_diff", "rest_days_diff"]


def data(rows_per_season: int = 40, seasons: int = 4) -> TrainingData:
    """`walk_forward` が通る最小の学習行列（3シーズン以上）。"""
    rng = np.random.default_rng(7)
    frames, season_ids, home_win, margins = [], [], [], []
    for index in range(seasons):
        elo = rng.normal(0.0, 80.0, rows_per_season)
        rest = rng.integers(0, 3, rows_per_season).astype(float)
        noise = rng.normal(0.0, 40.0, rows_per_season)
        frames.append(pd.DataFrame({"elo_diff": elo, "rest_days_diff": rest}))
        season_ids += [f"s{index}"] * rows_per_season
        home_win += list((elo + noise > 0).astype(float))
        # **得点差に分散を持たせる。** 残差 σ が 0 だと経路B が組めない
        margins += list(elo / 8.0 + noise / 4.0)
    features = pd.concat(frames, ignore_index=True)[COLUMNS]
    return TrainingData(
        features=features,
        home_win=np.asarray(home_win, dtype=np.float64),
        margin=np.asarray(margins, dtype=np.float64),
        total=np.full(len(features), 160.0),
        game_ids=[str(i) for i in range(len(features))],
        season_ids=season_ids,
        game_dates=[f"2020-01-{(i % 28) + 1:02d}" for i in range(len(features))],
        spectator_restricted=[None] * len(features),
    )


class FakeApi:
    """`metrics/active` と `models/:v/artifact` だけを返す。"""

    def __init__(self, *, version: str | None = "winner-v1.0.0",
                 artifact: str | None = None, fail: bool = False) -> None:
        self.version = version
        self.artifact = artifact
        self.fail = fail

    def get(self, path: str, _query: Any = None) -> object:
        if self.fail:
            raise LoaderError("404 / metrics/active")
        if path == "metrics/active":
            return {} if self.version is None else {"version": self.version}
        if path.endswith("/artifact"):
            return {"artifactText": self.artifact}
        raise AssertionError(path)


def artifact(columns: list[str], *, intercept: float = 0.0) -> str:
    size = len(columns)
    return dump_logistic(
        Logistic(intercept=intercept, coefficients=np.full(size, 0.01)),
        columns, np.zeros(size))


# --- 比較できる場合 ---


def test_the_current_model_is_evaluated_on_the_same_window() -> None:
    """**同一の walk-forward ウィンドウを通る**（分割器を2つ作らない）。

    `frozen_learner` を `walk_forward` に渡すため、ウィンドウは構造的に同一である。
    """
    training = data()
    api = FakeApi(artifact=artifact(COLUMNS))
    current = train.current_evaluation(
        api, training, weights=np.ones(len(training)))
    assert current.version == "winner-v1.0.0"
    assert current.evaluation is not None
    # 新しいモデルを同じ設定で評価したときと、**件数と実績が一致する**
    fresh = walk_forward(
        training, train.logistic_learner(), weights=np.ones(len(training)))
    assert current.evaluation.n == fresh.n
    assert np.array_equal(current.evaluation.actual, fresh.actual)


def test_the_frozen_learner_ignores_the_training_data() -> None:
    """**学習しない。** 保存済みの係数でそのまま予測する。"""
    model = Logistic(intercept=0.5, coefficients=np.array([0.01, 0.0]))
    learn = train.frozen_learner(model, COLUMNS)
    frame = pd.DataFrame({"elo_diff": [100.0], "rest_days_diff": [0.0]})
    predict, best = learn(frame, np.array([1.0]), np.array([1.0]), frame, np.array([1.0]))
    assert best == 0
    expected = model.predict(frame[COLUMNS].to_numpy(dtype=np.float64))
    assert np.allclose(predict(frame), expected)


# --- 比較できない場合（**黙って通さない**） ---


def test_a_missing_column_refuses_the_comparison() -> None:
    """**列が揃わなければ採用しない**（詳細設計 4.6）。

    列を削った変更がこれに当たる。**「比較できないときは通す」分岐を作らない。**
    """
    training = data()
    api = FakeApi(artifact=artifact([*COLUMNS, "消えた列"]))
    current = train.current_evaluation(
        api, training, weights=np.ones(len(training)))
    assert current.evaluation is None
    assert "--initial" in current.reason


def test_no_current_model_is_reported_as_such() -> None:
    training = data()
    current = train.current_evaluation(
        FakeApi(version=None), training, weights=np.ones(len(training)))
    assert (current.evaluation, current.version) == (None, None)
    assert current.reason == "現行モデルがない"


def test_a_failed_lookup_does_not_raise() -> None:
    """**照会の失敗でジョブを落とさない。** 比較しないだけである。"""
    training = data()
    current = train.current_evaluation(
        FakeApi(fail=True), training, weights=np.ones(len(training)))
    assert current.evaluation is None
    assert "照会に失敗" in current.reason


def test_initial_skips_the_comparison() -> None:
    """`--initial` が唯一の逃げ道である（既にある引数で、使ったことが残る）。"""
    training = data()
    current = train._current_for(
        FakeApi(artifact=artifact(COLUMNS)), training,
        weights=np.ones(len(training)), initial=True, max_folds=5)
    assert current.evaluation is None
    assert "--initial" in current.reason


def test_without_the_api_the_comparison_is_skipped() -> None:
    """登録しない実行では D1 に触らない（`API_BASE_URL` が無い手元でも回る）。"""
    training = data()
    current = train._current_for(
        None, training, weights=np.ones(len(training)), initial=False, max_folds=5)
    assert current.evaluation is None
    assert "登録しない" in current.reason


# --- 門として効いているか ---


def test_the_gate_sees_the_current_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """**条件1〜2 が課されること。**

    v1.97 まで `current_brier=None` を固定で渡しており、`passes_criteria` の
    条件1〜2（現行より良い / 有意かつ 0.003 以上）が一度も効いていなかった。
    """
    seen: dict[str, object] = {}
    original = train.passes_criteria

    def spy(inputs: Any) -> Any:
        seen["current_brier"] = inputs.current_brier
        seen["difference"] = inputs.difference
        return original(inputs)

    monkeypatch.setattr(train, "passes_criteria", spy)
    training = data()
    train.evaluate_all(
        training, api=FakeApi(artifact=artifact(COLUMNS)), log=lambda _m: None)
    assert isinstance(seen["current_brier"], float)
    assert seen["difference"] is not None


def test_the_gate_gets_nothing_when_the_comparison_is_impossible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """比較できないときは `current_brier` に `None` を渡す。

    **ただし `comparison_blocked` も渡す。** `None` だけでは「現行モデルがない
    （初回登録）」と区別できず、**条件1〜2 を課さずに採用してしまう**。
    """
    seen: dict[str, object] = {}
    original = train.passes_criteria

    def spy(inputs: Any) -> Any:
        seen["current_brier"] = inputs.current_brier
        seen["comparison_blocked"] = inputs.comparison_blocked
        return original(inputs)

    monkeypatch.setattr(train, "passes_criteria", spy)
    training = data()
    report = train.evaluate_all(
        training, api=FakeApi(artifact=artifact([*COLUMNS, "消えた列"])),
        log=lambda _m: None)
    assert seen["current_brier"] is None
    assert isinstance(seen["comparison_blocked"], str)
    assert not report.decision.adopt


def test_a_blocked_comparison_does_not_adopt(monkeypatch: pytest.MonkeyPatch) -> None:
    """**これが本命である。**

    v1.101 まで、比較できなかった場合は `current_brier=None` だけが渡り、
    `passes_criteria` が「初回登録」として条件1〜2 を**飛ばして採用していた**。
    `current_evaluation` のメッセージは「比較できないため採用しない」と言って
    いたのに、返した値は採用させていた。

    既存の検査はこれを捕まえられなかった — `current.evaluation is None` と
    `report.decision is not None` しか見ておらず、**採用されたかどうかを
    見ていなかった**。

    **n を下限（500）より上にする。** 下限を割ると `passes_criteria` が他の条件を
    見ずに返すため、**この検査が「標本が足りない」で通ってしまう**（実際に一度
    そうなった）。
    """
    training = data(rows_per_season=300)
    for api in (
        FakeApi(artifact=artifact([*COLUMNS, "消えた列"])),   # 列が揃わない
        FakeApi(fail=True),                                   # 照会に失敗した
        FakeApi(artifact="これは JSON ではない"),              # artifact が読めない
    ):
        report = train.evaluate_all(training, api=api, log=lambda _m: None)
        assert not report.decision.adopt
        assert any("比較できない" in f for f in report.decision.failures)
        assert report.comparison_blocked is not None


def test_not_comparing_on_purpose_is_not_blocked() -> None:
    """**`--initial` と「現行モデルがない」は `blocked` にしない。**

    どちらも条件1〜2 を課さずに通す経路であり、設計どおりの状態である
    （詳細設計 4.6）。ここを `blocked` にすると初回登録ができない。
    """
    training = data()
    weights = np.ones(len(training))
    assert not train._current_for(
        FakeApi(artifact=artifact(COLUMNS)), training,
        weights=weights, initial=True, max_folds=5).blocked
    assert not train._current_for(
        None, training, weights=weights, initial=False, max_folds=5).blocked
    assert not train.current_evaluation(
        FakeApi(version=None), training, weights=weights).blocked


def test_an_unreadable_current_model_is_blocked() -> None:
    """比較できない4つの経路すべてに `blocked` が立つ。"""
    training = data()
    weights = np.ones(len(training))
    for api in (
        FakeApi(fail=True),
        FakeApi(artifact="これは JSON ではない"),
        FakeApi(artifact=artifact([*COLUMNS, "消えた列"])),
    ):
        current = train.current_evaluation(api, training, weights=weights)
        assert current.evaluation is None
        assert current.blocked, current.reason
