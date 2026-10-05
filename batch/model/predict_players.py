"""チーム目標と個人スタッツの推論（詳細設計 4.2 の「チーム目標と個人スタッツ」）。

**4段を1試合ずつ通す。** 入力はスナップショットだけで（絶対ルール3）、モデルは
`GET /internal/models/active` が返す30本である。

```
TeamRate 14本（クラブごと）  →  reconcile_team_targets(予想スコア)   →  teamTargets 2件
第1段 候補集合（2.3.1）      →  PlayerAvail       →  P(出場)
第2段（P(出場) >= 0.5 の選手）→  PlayerMinutes     →  E[MIN | 出場]
第3段（同じ選手）            →  PlayerRate 14本   →  レートと成功率
                              →  reconcile(teamTargets)  →  playerPredictions
```

**順序は入れ替えられない。** 第3段の共有列に `pred_minutes`（第2段の出力）が入り、
整合化は `teamTargets`（前段を通した値）を要する。

**片側だけ出せる状態を作らない**（4.2）。片方のクラブで候補が空だったり整合化が
失敗した場合、その試合の個人スタッツは**両チームとも捨てる** — 画面は
「ホームだけフルボックススコアがある」状態を想定していない（5.3 の合計行は
両チーム分を並べる）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from batch.features import player_rate, team_rate
from batch.features.base import Context
from batch.model.reconcile import (
    InfeasibleTargetError,
    Reconciled,
    reconcile,
    reconcile_team_targets,
)
from batch.model.train_player import (
    AVAIL_DISPLAY_THRESHOLD,
    clamp_avail_prob,
    clip_minutes,
    shrink_column,
)
from batch.model.train_team_rates import clip_count, clip_pct, is_pct

#: 1チームの選手数の上限。**`player_predictions` の1リクエスト上限（120行）に
#: 収めるため**ではなく、候補集合が異常に膨らんだときの保険である（3.4 の上限は
#: 両チーム合わせて120行で、実データの候補は1チーム20人前後）。
MAX_PLAYERS_PER_CLUB = 40


class BoxScoreError(RuntimeError):
    """個人スタッツを組めない。**例外に本文を入れない**（絶対ルール4）。"""


@dataclass(frozen=True)
class SideBox:
    """片側（ホームまたはアウェイ）の結果。"""

    club_id: str
    #: 整合化を通したチーム目標（14項目）
    targets: Mapping[str, float]
    #: 整合化を通した選手予測
    players: Reconciled
    #: 選手ID → 出場確率（クランプ後）。`player_predictions.avail_prob` に書く
    avail: Mapping[str, float]


@dataclass(frozen=True)
class BoxScore:
    """1試合ぶん。**両チーム揃って初めて作る。**"""

    home: SideBox
    away: SideBox


def _frame(row: Mapping[str, float], columns: tuple[str, ...]) -> pd.DataFrame:
    """1行の表。**列順は定義が正**（辞書の並びに頼らない）。"""
    return pd.DataFrame([[float(row[k]) for k in columns]], columns=list(columns))


def team_targets(
    models: Any, context: Context, club_id: str, pred_score: float,
) -> dict[str, float]:
    """TeamRate 14本を回し、予想スコアへ整合化した目標を返す（2.4 の前段）。

    **丸めは推論側で当てる。** LightGBM の artifact には `clip_pct` /
    `clip_count` の層がない（学習時は `learner_for` が包んでいる）。
    """
    # **1行で14本ぶんの列が揃う**（`all_feature_keys()`）。14回作らない
    row = team_rate.team_rate_row(context, club_id)
    if row is None:
        raise BoxScoreError("TeamRate の特徴量が作れない（pace が欠けている）")
    raw: dict[str, float] = {}
    for target in team_rate.TARGETS:
        columns = team_rate.feature_keys(target)
        values = np.asarray(
            models.team_rate[target].predict(_frame(row, columns)), dtype=np.float64)
        raw[target] = float(
            clip_pct(values)[0] if is_pct(target) else clip_count(values)[0])
    # **予想スコアを正とし、成功率3項目を共通のロジットシフトで寄せる**（2.4）
    return reconcile_team_targets(raw, pred_score)


def _avail(models: Any, context: Context, club_id: str) -> dict[str, float]:
    """候補ごとの出場確率。**候補は過去の出場実績から作る**（2.3.1）。"""
    names = player_rate.candidates(context, club_id)
    if not names:
        raise BoxScoreError("候補が空である（季の1試合目で起きうる）")
    if len(names) > MAX_PLAYERS_PER_CLUB:
        raise BoxScoreError(f"候補が多すぎる: {len(names)}人")
    rows = [player_rate.avail_row(context, club_id, name) for name in names]
    frame = pd.DataFrame(
        [[float(r[k]) for k in player_rate.AVAIL_KEYS] for r in rows],
        columns=list(player_rate.AVAIL_KEYS),
    )
    probs = clamp_avail_prob(
        np.asarray(models.avail.predict(frame), dtype=np.float64))
    return {name: float(p) for name, p in zip(names, probs, strict=True)}


def _rate_row_for(
    context: Context, club_id: str, player_id: str, minutes: float,
) -> dict[str, float] | None:
    """第3段の1行。`pred_minutes` を第2段の出力で埋める（2.3.1）。"""
    row = player_rate.rate_row(context, club_id, player_id)
    if row is None:
        return None
    out = dict(row)
    out["pred_minutes"] = float(minutes)
    return out


def _rate_values(
    models: Any, target: str, rows: list[dict[str, float]],
) -> np.ndarray:
    """第3段の1項目をまとめて予測する。**シュリンクは推論側で当てる**（2.3.1）。"""
    columns = player_rate.rate_model_keys(target)
    pct = not (target in team_rate.COUNT_TARGETS)
    frame = pd.DataFrame(index=range(len(rows)))
    if pct:
        k = models.shrink_k[target]
        prior = np.array([r[f"{target}_prior"] for r in rows], dtype=np.float64)
        shrunk = {
            f"{target}_shrunk_l10": shrink_column(
                np.array([r[f"{target}_made_l10"] for r in rows], dtype=np.float64),
                np.array([r[f"{target}_att_l10"] for r in rows], dtype=np.float64),
                prior, k),
            f"{target}_shrunk_season": shrink_column(
                np.array([r[f"{target}_made_season"] for r in rows], dtype=np.float64),
                np.array([r[f"{target}_att_season"] for r in rows], dtype=np.float64),
                prior, k),
        }
    else:
        shrunk = {}
    for column in columns:
        if column in shrunk:
            frame[column] = shrunk[column]
        else:
            frame[column] = np.array(
                [r[column] for r in rows], dtype=np.float64)
    values = np.asarray(
        models.player_rate[target].predict(frame[list(columns)]), dtype=np.float64)
    return np.clip(values, 0.01, 0.99) if pct else np.maximum(values, 0.0)


def side_box(
    models: Any, context: Context, club_id: str, pred_score: float,
) -> SideBox:
    """片側を通す。**失敗したら例外を投げる**（呼び出し側が両チームとも捨てる）。"""
    targets = team_targets(models, context, club_id, pred_score)
    avail = _avail(models, context, club_id)
    # **`P(出場) < 0.5` の選手は進めない**（要件 6.8.4）
    picked = [
        name for name, prob in avail.items() if prob >= AVAIL_DISPLAY_THRESHOLD]
    if not picked:
        raise BoxScoreError("出場確率 0.5 以上の選手が1人もいない")

    # **`minutes_row` は1人1回だけ呼ぶ。** `None`（過去が1試合も無い）の選手は
    # 第2段も第3段も出せないため、ここで落とす（4.2 の表）
    minutes_rows = [
        (name, player_rate.minutes_row(context, club_id, name)) for name in picked]
    usable = [(name, row) for name, row in minutes_rows if row is not None]
    if not usable:
        raise BoxScoreError("第2段の特徴量が作れる選手が1人もいない")
    minutes_frame = pd.DataFrame(
        [[float(row[k]) for k in player_rate.MINUTES_KEYS] for _, row in usable],
        columns=list(player_rate.MINUTES_KEYS),
    )
    minutes = clip_minutes(
        np.asarray(models.minutes.predict(minutes_frame), dtype=np.float64))

    rows: list[dict[str, float]] = []
    names: list[str] = []
    kept: dict[str, float] = {}
    for (name, _), value in zip(usable, minutes, strict=True):
        row = _rate_row_for(context, club_id, name, float(value))
        if row is None:
            # **その選手だけ落とす**（4.2 の表）。残りで整合化する
            continue
        rows.append(row)
        names.append(name)
        kept[name] = float(value)
    if not rows:
        raise BoxScoreError("第3段の特徴量が作れる選手が1人もいない")

    predicted = {
        target: _rate_values(models, target, rows) for target in team_rate.TARGETS}
    players = [
        player_rate_input(name, avail[name], kept[name], predicted, index)
        for index, name in enumerate(names)
    ]
    return SideBox(
        club_id=club_id, targets=targets,
        players=reconcile(players, targets),
        avail={name: avail[name] for name in names},
    )


def player_rate_input(
    player_id: str, avail_prob: float, minutes: float,
    predicted: Mapping[str, np.ndarray], index: int,
) -> Any:
    """`reconcile` が受け取る1人ぶん（`PlayerRates`）。"""
    from batch.model.reconcile import PlayerRates

    return PlayerRates(
        player_id=player_id,
        avail_prob=avail_prob,
        minutes_if_plays=minutes,
        rate={
            target: float(predicted[target][index])
            for target in team_rate.COUNT_TARGETS
        },
        pct={
            name: float(predicted[name][index])
            for name, _, _ in team_rate.PCT_TARGETS
        },
    )


def predict_box_score(
    models: Any, context: Context, *, home_score: float, away_score: float,
) -> BoxScore:
    """1試合ぶん。**両チーム揃って初めて返す**（片側だけ出さない。4.2）。

    `InfeasibleTargetError` / `BoxScoreError` は呼び出し側が捕まえ、
    **チーム予測のみ保存して個人スタッツを破棄する**（2.4）。
    """
    return BoxScore(
        home=side_box(models, context, context.home_club_id, home_score),
        away=side_box(models, context, context.away_club_id, away_score),
    )


__all__ = [
    "BoxScore",
    "BoxScoreError",
    "InfeasibleTargetError",
    "SideBox",
    "predict_box_score",
    "side_box",
    "team_targets",
]
