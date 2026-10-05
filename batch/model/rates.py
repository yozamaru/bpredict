"""30本（TEAM_RATE 14 / PLAYER_AVAIL / PLAYER_MIN / PLAYER_RATE 14）の評価。

**評価だけを行う。** 最終当てはめと `model_versions` の行の組み立ては
`batch/model/final.py`、行列の構築とキャッシュと登録は `batch/jobs/train.py` が
受け持つ。

**分割器を2つ作らない。** 各モデルの `evaluate_*` はいずれも `walk_forward` を
通っており（`evaluate.py`）、ここはそれを順に呼ぶだけである。

**選手モデルの fold 数は3である**（要件 6.8.7）。`PLAYER_MAX_FOLDS` が既定で、
TeamRate は勝敗と同じ 5 を使う（要件 6.8.7 が「チームモデルは5」と定めている）。
"""

from __future__ import annotations

import time
from collections.abc import Callable

from batch.features.team_rate import TARGETS
from batch.model import train_player, train_team_rates
from batch.model.dataset import PlayerAvailData, PlayerMinutesData, PlayerRateData, TeamRateData
from batch.model.evaluate import MAX_FOLDS, Evaluation
from batch.model.final import RateEvaluations
from batch.model.params import PLAYER_MAX_FOLDS


def evaluate_rates(
    *,
    team_data: TeamRateData,
    avail_data: PlayerAvailData,
    minutes_data: PlayerMinutesData,
    rate_data: PlayerRateData,
    team_max_folds: int = MAX_FOLDS,
    player_max_folds: int = PLAYER_MAX_FOLDS,
    log: Callable[[str], None] = print,
) -> RateEvaluations:
    """30本を同一ウィンドウで評価し、ベースラインも同じ fold で測る。

    **ベースラインを必ず測る。** 門ではないが（要件 6.5 の射程）、
    `model_versions.notes` に残すために要る — どのモデルが学習に値したのかを
    後から辿れるようにする。

    **第2段の当てはめは `MinutesProvider` で使い回す。** 14本が fold ごとに
    同じ1本を共有する（2.3.1）。
    """
    team: dict[str, Evaluation] = {}
    team_baseline: dict[str, Evaluation] = {}
    for target in TARGETS:
        started = time.monotonic()
        team[target] = train_team_rates.evaluate_target(
            team_data, target, max_folds=team_max_folds)
        team_baseline[target] = train_team_rates.evaluate_baseline(
            team_data, target, max_folds=team_max_folds)
        log(
            f"rates: TEAM_RATE/{target} MAE {team[target].mae:.4f}"
            f" / 平均 {team_baseline[target].mae:.4f}"
            f"（{time.monotonic() - started:.0f}秒）"
        )

    started = time.monotonic()
    avail = train_player.evaluate_avail(avail_data, max_folds=player_max_folds)
    avail_baseline = train_player.evaluate_avail(
        avail_data, max_folds=player_max_folds,
        learner=train_player.ratio_learner())
    log(
        f"rates: PLAYER_AVAIL Brier {avail.brier:.6f}"
        f" / 直近10試合の出場率 {avail_baseline.brier:.6f}"
        f" / ECE {avail.ece:.6f}（{time.monotonic() - started:.0f}秒）"
    )

    started = time.monotonic()
    minutes = train_player.evaluate_minutes(minutes_data, max_folds=player_max_folds)
    minutes_baseline = train_player.evaluate_baseline(
        minutes_data, max_folds=player_max_folds,
        learner=train_player.recent_learner())
    log(
        f"rates: PLAYER_MIN MAE {minutes.mae:.4f}"
        f" / 直近5試合の平均 {minutes_baseline.mae:.4f}"
        f"（{time.monotonic() - started:.0f}秒）"
    )

    # **第2段の当てはめを14本で使い回す**（2.3.1）。省くと fold ごとに14回当てはめる
    provider = train_player.MinutesProvider(rate_data)
    player: dict[str, Evaluation] = {}
    player_baseline: dict[str, Evaluation] = {}
    for target in TARGETS:
        started = time.monotonic()
        k = train_player.SHRINK_K.get(target, train_player.SHRINK_K_INITIAL)
        # **成功率は実現値（`made / att`）に対して測る**（2.3.1）。シュリンク済みの
        # 目的変数に対する MAE は `k` を上げるだけで下がり、**学習しない
        # ベースラインとの比較が人工物になる** — 実測で `fg3_pct` が +42% 良いと
        # 出たが、実現値に対して測ると −0.15% で負けていた。カウント11項目は
        # `realized` の影響を受けない（`evaluate_rate` が成功率だけに当てる）
        player[target] = train_player.evaluate_rate(
            rate_data, target, k=k, max_folds=player_max_folds, provider=provider,
            realized=True)
        player_baseline[target] = train_player.evaluate_rate(
            rate_data, target, k=k, max_folds=player_max_folds, provider=provider,
            learner=train_player.rate_recent_learner(target), realized=True)
        log(
            f"rates: PLAYER_RATE/{target} MAE {player[target].mae:.6f}"
            f" / 直近10試合の水準 {player_baseline[target].mae:.6f}"
            f"（{time.monotonic() - started:.0f}秒）"
        )

    return RateEvaluations(
        team_rate=team, team_rate_baseline=team_baseline,
        avail=avail, avail_baseline=avail_baseline,
        minutes=minutes, minutes_baseline=minutes_baseline,
        player_rate=player, player_rate_baseline=player_baseline,
    )
