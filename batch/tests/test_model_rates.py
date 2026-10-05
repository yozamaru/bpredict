"""30本の評価の結線（詳細設計 4.5.1 / 2.3.1）。

| テスト | どの規約か |
|---|---|
| `test_pct_items_are_measured_against_the_realized_rate` | **`k` の人工物で比較しない**（2.3.1） |
| `test_the_stage_two_fit_is_shared` | 第2段を14本で使い回す（2.3.1） |
| `test_team_rate_uses_five_folds` | チームは5、選手は3（要件 6.8.7） |

**46本の当てはめを CI で回さない。** `train_*` の `evaluate_*` を差し替え、
**どう呼ばれるか**だけを見る（評価そのものは各モジュールのテストが見る）。
"""
from __future__ import annotations

from typing import Any

import numpy as np

from batch.features.team_rate import TARGETS
from batch.model import rates
from batch.model.evaluate import Evaluation, Fold


def one_fold() -> Evaluation:
    """`log()` の f-string が `mae` / `brier` / `ece` を読むため、**空では足りない。**

    等頻度10ビンにビンあたり50件を満たす大きさにする（要件 6.4）。
    """
    probs = np.linspace(0.05, 0.95, 600)
    actual = (np.arange(600) % 2).astype(np.float64)
    return Evaluation(folds=(Fold(
        test_season="s4", train_seasons=("s1", "s2"), valid_season="s3",
        n_train=100, n_valid=50, n_test=600, best_iteration=20,
        probs=probs, actual=actual,
    ),))


def stub(monkeypatch: Any) -> list[dict[str, Any]]:
    """`evaluate_*` を記録するだけの関数に差し替える。"""
    calls: list[dict[str, Any]] = []
    empty = one_fold()

    def record(name: str) -> Any:
        def call(*args: Any, **kwargs: Any) -> Evaluation:
            calls.append({"name": name, "args": args, **kwargs})
            return empty
        return call

    monkeypatch.setattr(
        rates.train_team_rates, "evaluate_target", record("team"))
    monkeypatch.setattr(
        rates.train_team_rates, "evaluate_baseline", record("team_baseline"))
    monkeypatch.setattr(rates.train_player, "evaluate_avail", record("avail"))
    monkeypatch.setattr(rates.train_player, "evaluate_minutes", record("minutes"))
    monkeypatch.setattr(rates.train_player, "evaluate_baseline", record("minutes_base"))
    monkeypatch.setattr(rates.train_player, "evaluate_rate", record("rate"))
    monkeypatch.setattr(
        rates.train_player, "MinutesProvider", lambda data, **_: object())
    monkeypatch.setattr(rates.train_player, "ratio_learner", lambda: object())
    monkeypatch.setattr(rates.train_player, "recent_learner", lambda: object())
    monkeypatch.setattr(
        rates.train_player, "rate_recent_learner", lambda target: object())
    return calls


def run(monkeypatch: Any) -> list[dict[str, Any]]:
    calls = stub(monkeypatch)
    rates.evaluate_rates(
        team_data=object(), avail_data=object(),  # type: ignore[arg-type]
        minutes_data=object(), rate_data=object(),  # type: ignore[arg-type]
        log=lambda _: None,
    )
    return calls


def test_pct_items_are_measured_against_the_realized_rate(monkeypatch: Any) -> None:
    """**成功率は実現値（`made / att`）に対して測る**（2.3.1）。

    シュリンク済みの目的変数に対する MAE は `k` を上げるだけで下がるため、
    **学習しないベースラインとの比較が人工物になる** — 実測で `fg3_pct` が
    +42% 良いと出たが、実現値に対して測ると −0.15% で負けていた。
    """
    calls = [c for c in run(monkeypatch) if c["name"] == "rate"]
    assert len(calls) == len(TARGETS) * 2
    assert all(c["realized"] is True for c in calls)


def test_the_stage_two_fit_is_shared(monkeypatch: Any) -> None:
    """14本 × 2（モデルとベースライン）が**同じ `provider`** を受け取ること。"""
    calls = [c for c in run(monkeypatch) if c["name"] == "rate"]
    providers = {id(c["provider"]) for c in calls}
    assert len(providers) == 1


def test_team_rate_uses_five_folds(monkeypatch: Any) -> None:
    """**チームは5、選手は3**（要件 6.8.7）。既定を取り違えない。"""
    calls = run(monkeypatch)
    team = [c["max_folds"] for c in calls if c["name"].startswith("team")]
    player = [
        c["max_folds"] for c in calls
        if c["name"] in {"avail", "minutes", "minutes_base", "rate"}
    ]
    assert set(team) == {rates.MAX_FOLDS}
    assert set(player) == {rates.PLAYER_MAX_FOLDS}
