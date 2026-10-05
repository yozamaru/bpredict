"""チーム目標と個人スタッツの推論（詳細設計 4.2 の「チーム目標と個人スタッツ」）。

**boosters は偽物を使う。** ここで検査するのは4段の順序と、片側だけ出さない
こと、失敗の扱いである — LightGBM が何を返すかは学習側のテストの仕事である。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
import pytest

from batch.features import player_rate, team_rate
from batch.features.base import Context, build_context
from batch.features.dataset import export_sqlite
from batch.model.predict_players import (
    BoxScore,
    BoxScoreError,
    predict_box_score,
    side_box,
    team_targets,
)
from batch.model.reconcile import InfeasibleTargetError, derived_points

SEASON = "2025-26-B1"


@dataclass
class FakeBooster:
    """固定値を返す booster。`feature_name()` は空（列名の照合を通す）。"""

    value: float

    def predict(self, frame: pd.DataFrame) -> np.ndarray:
        return np.full(len(frame), self.value, dtype=np.float64)

    def feature_name(self) -> list[str]:
        return []


@dataclass
class FakeRateModels:
    """`RateModels` と同じ属性を持つ偽物。"""

    team_rate: dict[str, Any]
    avail: Any
    minutes: Any
    player_rate: dict[str, Any]
    shrink_k: dict[str, float]
    versions: dict[tuple[str, str], str]


def fake_models(
    *, avail: float = 0.9, minutes: float = 20.0,
    count: float = 30.0, pct: float = 0.5, rate: float = 0.2,
) -> FakeRateModels:
    """14本ぶんの偽 booster を揃える。

    カウントは試投数・リバウンド等の**水準**、`rate` は per-minute の**レート**。

    **`count` の既定は 30 である。** 到達できる得点の上限は
    `2·fg2a + 3·fg3a + fta`（2.4）で、30 なら 180点 — 実データのチーム得点
    （約79点）を含む。小さすぎると `InfeasibleTargetError` になる。
    """
    pct_names = {name for name, _, _ in team_rate.PCT_TARGETS}
    return FakeRateModels(
        team_rate={
            t: FakeBooster(pct if t in pct_names else count)
            for t in team_rate.TARGETS
        },
        avail=FakeBooster(avail),
        minutes=FakeBooster(minutes),
        player_rate={
            t: FakeBooster(pct if t in pct_names else rate)
            for t in team_rate.TARGETS
        },
        shrink_k={name: 20.0 for name in pct_names},
        versions={("PLAYER_AVAIL", ""): "player_avail-v1.0.0"},
    )


def _late_game(con: sqlite3.Connection) -> tuple[str, datetime]:
    """その季の終盤の試合（両クラブに過去の出場実績が溜まっている）。"""
    row = con.execute(
        "SELECT id, tipoff_at FROM games"
        " WHERE status = 'FINISHED' AND season_id = ?"
        " ORDER BY game_date DESC, tipoff_at DESC LIMIT 1",
        (SEASON,),
    ).fetchone()
    return str(row[0]), datetime.fromisoformat(str(row[1]))


@pytest.fixture
def context(seeded_db: sqlite3.Connection) -> Context:
    game_id, as_of = _late_game(seeded_db)
    return build_context(game_id, as_of, export_sqlite(seeded_db))


# --- チーム目標 ---

def test_team_targets_match_the_predicted_score(context: Context) -> None:
    """**チーム目標の導出得点が予想スコアと一致する**（A-16 / 2.4 の前段）。"""
    targets = team_targets(fake_models(), context, context.home_club_id, 84.0)
    assert derived_points(targets) == pytest.approx(84.0, abs=1e-6)


def test_team_targets_do_not_move_the_attempts(context: Context) -> None:
    """**試投数は動かさない**（2.4）。帳尻は成功率だけで合わせる。"""
    models = fake_models(count=25.0)
    targets = team_targets(models, context, context.home_club_id, 70.0)
    for name in ("fg2a", "fg3a", "fta"):
        assert targets[name] == pytest.approx(25.0)


def test_an_unreachable_score_raises(context: Context) -> None:
    """**到達不能な目標で `InfeasibleTargetError`**（2.4）。無言でクリップしない。

    試投数を小さくすると、成功率を 1 にしても届かない得点が作れる。
    """
    models = fake_models(count=1.0)
    with pytest.raises(InfeasibleTargetError):
        team_targets(models, context, context.home_club_id, 200.0)


# --- 選手側 ---

def test_players_reconcile_to_the_team_targets(context: Context) -> None:
    """**選手の合計がチーム目標に一致する**（A-13 / 2.4）。"""
    side = side_box(fake_models(), context, context.home_club_id, 84.0)
    total = float(side.players.points.sum())
    assert total == pytest.approx(84.0, abs=0.5)
    assert float(side.players.minutes.sum()) == pytest.approx(200.0, abs=0.5)
    for name in ("fg2a", "fg3a", "fta", "oreb", "ast"):
        assert float(side.players.counts[name].sum()) == pytest.approx(
            side.targets[name], rel=0.02)


def test_players_below_the_threshold_are_dropped(context: Context) -> None:
    """**`P(出場) < 0.5` の選手は進めない**（要件 6.8.4）。"""
    with pytest.raises(BoxScoreError, match="0.5"):
        side_box(fake_models(avail=0.2), context, context.home_club_id, 84.0)


def test_avail_is_clamped_to_the_player_bounds(context: Context) -> None:
    """出場確率は `[0.01, 0.99]`。**勝率の `[0.05, 0.95]` を借りない**（2.3.1）。"""
    side = side_box(fake_models(avail=1.5), context, context.home_club_id, 84.0)
    assert all(value == pytest.approx(0.99) for value in side.avail.values())


def test_both_sides_or_neither(context: Context) -> None:
    """**両チーム揃って初めて返す**（4.2）。片側だけの `BoxScore` を作らない。"""
    box = predict_box_score(
        fake_models(), context, home_score=84.0, away_score=78.0)
    assert isinstance(box, BoxScore)
    assert box.home.club_id == context.home_club_id
    assert box.away.club_id == context.away_club_id
    assert float(box.away.players.points.sum()) == pytest.approx(78.0, abs=0.5)


def test_the_pct_output_is_clipped(context: Context) -> None:
    """成功率は `[0.01, 0.99]` に収める（要件 6.8.4。整合化が logit を通る）。"""
    side = side_box(
        fake_models(pct=5.0, count=60.0), context, context.home_club_id, 84.0)
    for name, _, _ in team_rate.PCT_TARGETS:
        values = side.players.pcts[name]
        assert float(values.min()) > 0.0
        assert float(values.max()) < 1.0


def test_candidates_come_from_past_appearances(context: Context) -> None:
    """**候補は過去の出場実績から作る**（2.3.1）。`player_seasons` を使わない。

    候補の全員が、そのクラブで**対象試合より前に**出場していることを確かめる
    （`player_seasons` を使っていればこれを満たさない選手が混じりうる）。
    """
    names = player_rate.candidates(context, context.home_club_id)
    assert names
    stats = context.dataset.table("player_game_stats")
    games = context.dataset.table("games")
    finished = set(
        games.loc[games["game_date"] < context.game_date, "id"].astype(str))
    past = set(
        stats.loc[
            (stats["club_id"].astype(str) == context.home_club_id)
            & stats["game_id"].astype(str).isin(finished),
            "player_id",
        ].astype(str)
    )
    assert set(names) <= past
    side = side_box(fake_models(), context, context.home_club_id, 84.0)
    assert set(side.avail) <= set(names)
