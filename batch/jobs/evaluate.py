"""結果照合と的中率の集計（詳細設計 4.12。4.2 のステップ5〜6）。

    python -m batch.jobs.evaluate [--dry-run] [--limit N]

**実績はスナップショットから引く。** `GET /internal/predictions/pending` が返すのは
予測の側だけで、実績スコアを含まない（3.4）。`daily_ingest` はスナップショットを
書いてから照合するため、同一の run では必ず揃う。**D1 の `games` を読まない**
（絶対ルール3の「入力データ」にあたる）。

**`VOID` は的中率の母数から除外する**（受け入れ基準 A-04）。中止・延期の行は
`actual_home_win` / `is_correct` / `brier` / `score_mae` を **NULL** にする。
0 を入れると「ホームが負けた」という意味を持ってしまう。
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from batch.features.dataset import load_snapshot
from batch.loader.api import InternalApi, LoaderError
from batch.loader.limits import max_rows_per_request

DEFAULT_SNAPSHOT = Path("batch/snapshot")

#: `prediction_results` は14列 → floor(100/14)=7 行/文 × 40 = 280（3.4）
RESULTS_PER_REQUEST = max_rows_per_request("prediction_results")
#: `accuracy_summary` は9列 → floor(100/9)=11 行/文 × 40 = 440
SUMMARY_PER_REQUEST = max_rows_per_request("accuracy_summary")

#: 確率のビンの数（`prob_bucket` は 0〜9。詳細設計 1.6）
BUCKETS = 10

#: モデル横断の集計では空文字を入れる。**NULL にしない**（詳細設計 1.6）
ACROSS_MODELS = ""


class EvaluateError(RuntimeError):
    """照合できない入力。黙って既定値を入れない。"""


@dataclass(frozen=True)
class Result:
    """`prediction_results` の1行。"""

    prediction_id: str
    game_id: str
    season_id: str
    model_version: str
    home_win_prob: float
    prob_bucket: int
    outcome: str
    was_provisional: int
    predicted_home_win: int | None = None
    actual_home_win: int | None = None
    is_correct: int | None = None
    brier: float | None = None
    score_mae: float | None = None

    @property
    def counted(self) -> bool:
        """的中率の母数に入るか。**`VOID` は入らない**（A-04）。"""
        return self.outcome != "VOID"


@dataclass
class Outcome:
    results: list[Result] = field(default_factory=list)
    #: スナップショットに実績が無く飛ばした予測（試合IDと理由）
    skipped: list[tuple[str, str]] = field(default_factory=list)
    #: **この回で照合した、母数に入る試合の最も新しい `game_date`**（詳細設計 3.7）。
    #: `/results`（引数なし）が既定で見る日になる。**`VOID` は数えない** —
    #: 中止・延期の試合は「実績と予測の対比」として出す対象がない（3.3）
    latest_result_date: str | None = None


def bucket_of(prob: float) -> int:
    """`min(floor(p × 10), 9)`。**`p = 1.0` を10番目にしない**（4.12）。"""
    return min(math.floor(prob * BUCKETS), BUCKETS - 1)


def bucket_label(bucket: int) -> str:
    """`60-70%` の形。**公開APIがこの文字列をそのまま画面に出す**（3.3）。"""
    return f"{bucket * 10}-{(bucket + 1) * 10}%"


def _number(value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(str(value))
    except ValueError:
        return None
    return None if math.isnan(number) else number


def evaluate_one(prediction: dict[str, object], game: dict[str, object]) -> Result:
    """1件を照合する。**実績が無ければ `VOID` にする**（推測しない）。

    勝敗の閾値は `p > 0.5`。**`batch/model/metrics.py` の `accuracy()` と同じ** —
    同じ閾値を2箇所で別に決めると、的中率ページの数字が採用判定の数字と合わない。
    """
    prob = _number(prediction.get("homeWinProb"))
    if prob is None or not 0.0 <= prob <= 1.0:
        raise EvaluateError("home_win_prob が 0〜1 の数値でない")

    status = str(game.get("status") or "")
    home = _number(game.get("home_score"))
    away = _number(game.get("away_score"))

    outcome = "VOID"
    actual: int | None = None
    predicted: int | None = None
    correct: int | None = None
    score: float | None = None
    mae: float | None = None
    if status == "FINISHED" and home is not None and away is not None:
        actual = 1 if home > away else 0
        predicted = 1 if prob > 0.5 else 0
        outcome = "WIN" if actual else "LOSS"
        correct = 1 if predicted == actual else 0
        score = (prob - actual) ** 2
        mae = _score_mae(prediction, home, away)
    # **それ以外（中止・延期）は列を NULL のままにする**（0 を入れない。A-04）

    return Result(
        prediction_id=str(prediction["predictionId"]),
        game_id=str(prediction["gameId"]),
        season_id=str(prediction["seasonId"]),
        model_version=str(prediction["modelVersion"]),
        home_win_prob=prob,
        prob_bucket=bucket_of(prob),
        outcome=outcome,
        was_provisional=1 if _number(prediction.get("wasProvisional")) else 0,
        predicted_home_win=predicted,
        actual_home_win=actual,
        is_correct=correct,
        brier=score,
        score_mae=mae,
    )


def _score_mae(
    prediction: dict[str, object], home: float, away: float,
) -> float | None:
    """**両チーム得点の絶対誤差の平均**（4.12）。得点差の MAE と混同しない。

    予想スコアが保存されていなければ None（0 で埋めない）。
    """
    pred_home = _number(prediction.get("predHomeScore"))
    pred_away = _number(prediction.get("predAwayScore"))
    if pred_home is None or pred_away is None:
        return None
    return (abs(pred_home - home) + abs(pred_away - away)) / 2.0


def evaluate(
    predictions: Sequence[dict[str, object]], games: pd.DataFrame,
) -> Outcome:
    """照合対象をまとめて処理する。

    **スナップショットに無い試合、状態が食い違う試合は飛ばして報告する**（4.12）。
    状態を推測しない。
    """
    needed = ("id", "status", "home_score", "away_score", "game_date")
    missing = [c for c in needed if c not in games.columns]
    if missing:
        raise EvaluateError(f"games に必要な列がない: {missing}")
    by_id = {
        str(record["id"]): {str(k): v for k, v in record.items()}
        for record in games.to_dict("records")
    }

    out = Outcome()
    for prediction in predictions:
        game_id = str(prediction["gameId"])
        game = by_id.get(game_id)
        if game is None:
            out.skipped.append((game_id, "スナップショットに試合が無い"))
            continue
        status = str(game.get("status") or "")
        if status == "SCHEDULED":
            # D1 では終了しているがスナップショットが古い。**推測しない**
            out.skipped.append((game_id, "スナップショットが SCHEDULED のまま"))
            continue
        result = evaluate_one(prediction, game)
        out.results.append(result)
        # **母数に入る試合だけを数える**（`VOID` は `/results` に出ない。3.3）
        if result.counted:
            date = str(game.get("game_date") or "")
            if date and (out.latest_result_date is None
                         or date > out.latest_result_date):
                out.latest_result_date = date
    return out


# --- accuracy_summary ---


@dataclass(frozen=True)
class Summary:
    scope: str
    scope_key: str
    model_version: str
    n: int
    accuracy: float
    brier: float
    actual_rate: float | None = None
    baseline_accuracy: float | None = None
    #: 予想スコアの誤差（**1チームあたり**の平均絶対誤差）。母数は `n` と同じ。
    #: **食い違う場合は None**（4.12。母数の列を2つ持たない）
    score_mae: float | None = None
    #: **その帯の的中率。`BUCKET` 行だけが持つ**（1.6 / 4.12）。
    #: 他のスコープでは `accuracy` がそのまま的中率であり、同じ値を2列に持たない。
    #: **`actual_rate` で代用できない** — あれはホームが勝った割合で、
    #: 50%未満の帯では的中率と符号が逆になる（本番で6試合中3試合が逆に出た）
    hit_rate: float | None = None


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _score_mae_of(rows: Sequence[Result]) -> float | None:
    """**全行に予想スコアがあるときだけ**平均を返す（4.12）。

    欠けた行を除いて平均すると、画面の「312試合中…」の隣に**母数の違う数字が
    並ぶ**。`VOID` は呼び出し側で既に除かれているため、ここで欠けるのは
    **予想スコア未保存**の行だけである（上流の欠陥であり、黙って平均しない）。
    """
    values = [r.score_mae for r in rows if r.score_mae is not None]
    if len(values) != len(rows):
        return None
    return None if not values else _mean(values)


def _group(rows: Sequence[Result], scope: str, scope_key: str,
           model_version: str) -> Summary | None:
    """1スコープの集計。**母数が0なら行を作らない**（4.12）。

    `accuracy` と `brier` は DDL で NOT NULL であり、0件の平均は定義できない。
    """
    counted = [r for r in rows if r.counted]
    if not counted:
        return None
    return Summary(
        scope=scope, scope_key=scope_key, model_version=model_version,
        n=len(counted),
        accuracy=_mean([float(r.is_correct or 0) for r in counted]),
        brier=_mean([float(r.brier or 0.0) for r in counted]),
        score_mae=_score_mae_of(counted),
    )


def summarize(results: Sequence[Result]) -> list[Summary]:
    """5スコープを作る（4.12 の表）。**全行を作り直す前提である。**"""
    counted = [r for r in results if r.counted]
    if not counted:
        return []

    rows: list[Summary] = []

    # OVERALL — `baseline_accuracy` はここにだけ入れる（「ホームが必ず勝つ」）
    overall = _group(counted, "OVERALL", "all", ACROSS_MODELS)
    if overall is not None:
        rows.append(Summary(
            scope=overall.scope, scope_key=overall.scope_key,
            model_version=overall.model_version, n=overall.n,
            accuracy=overall.accuracy, brier=overall.brier,
            score_mae=overall.score_mae,
            baseline_accuracy=_mean(
                [float(r.actual_home_win or 0) for r in counted]),
        ))

    for season_id in sorted({r.season_id for r in counted}):
        row = _group([r for r in counted if r.season_id == season_id],
                     "SEASON", season_id, ACROSS_MODELS)
        if row is not None:
            rows.append(row)

    # MODEL — **`model_version` に実際の版を入れる**（横断の4スコープだけが空文字）
    for version in sorted({r.model_version for r in counted}):
        row = _group([r for r in counted if r.model_version == version],
                     "MODEL", version, version)
        if row is not None:
            rows.append(row)

    # BUCKET — **3つの意味を3つの列に分ける**（4.12）。
    #   accuracy    = 予想した確率の平均       （較正曲線の横軸）
    #   actual_rate = ホームが勝った割合       （較正曲線の縦軸）
    #   hit_rate    = **その帯の的中率**       （/results の「位置づけ」）
    #
    # **`hit_rate` を足すまで `/results` は `actual_rate` を的中率として出していた。**
    # 50%未満の帯では予測が「アウェイ勝ち」なので符号が反転し、27%と予想して
    # アウェイが勝った試合に「0.0%が的中」と出ていた（2026-10-08 の本番で3件）。
    for bucket in sorted({r.prob_bucket for r in counted}):
        inside = [r for r in counted if r.prob_bucket == bucket]
        rows.append(Summary(
            scope="BUCKET", scope_key=bucket_label(bucket),
            model_version=ACROSS_MODELS, n=len(inside),
            accuracy=_mean([r.home_win_prob for r in inside]),
            brier=_mean([float(r.brier or 0.0) for r in inside]),
            actual_rate=_mean([float(r.actual_home_win or 0) for r in inside]),
            hit_rate=_mean([float(r.is_correct or 0) for r in inside]),
            score_mae=_score_mae_of(inside),
        ))

    for key, flag in (("provisional", 1), ("confirmed", 0)):
        row = _group([r for r in counted if r.was_provisional == flag],
                     "PROVISIONAL", key, ACROSS_MODELS)
        if row is not None:
            rows.append(row)

    return rows


# --- 送信 ---


def _result_payload(results: Sequence[Result]) -> dict[str, object]:
    return {"results": [
        {
            "predictionId": r.prediction_id, "gameId": r.game_id,
            "seasonId": r.season_id, "modelVersion": r.model_version,
            "homeWinProb": r.home_win_prob, "probBucket": r.prob_bucket,
            "outcome": r.outcome, "wasProvisional": r.was_provisional,
            "predictedHomeWin": r.predicted_home_win,
            "actualHomeWin": r.actual_home_win,
            "isCorrect": r.is_correct, "brier": r.brier,
            "scoreMae": r.score_mae,
        }
        for r in results
    ]}


def _summary_payload(rows: Sequence[Summary]) -> dict[str, object]:
    return {"rows": [
        {
            "scope": s.scope, "scopeKey": s.scope_key,
            "modelVersion": s.model_version, "n": s.n,
            "accuracy": s.accuracy, "brier": s.brier,
            "actualRate": s.actual_rate,
            "baselineAccuracy": s.baseline_accuracy,
            "scoreMae": s.score_mae,
            "hitRate": s.hit_rate,
        }
        for s in rows
    ]}


def send(api: InternalApi, results: Sequence[Result],
         summaries: Sequence[Summary]) -> None:
    """照合結果と集計を送る。**行数上限はテーブルごとに守る**（3.4）。"""
    for start in range(0, len(results), RESULTS_PER_REQUEST):
        api.post("evaluate", _result_payload(
            results[start:start + RESULTS_PER_REQUEST]))
    # **`accuracy_summary` は洗い替えである。** 1リクエストで送れるなら分割しない
    if len(summaries) > SUMMARY_PER_REQUEST:
        raise EvaluateError(
            f"集計の行数が1リクエストの上限を超えている: {len(summaries)}")
    api.post("summary", _summary_payload(summaries))


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _log(api: InternalApi, status: str, rows: int) -> None:
    try:
        api.post("log", {
            "id": f"evaluate-{uuid.uuid4().hex[:8]}",
            "job": "evaluate",
            "startedAt": _iso_now(), "finishedAt": _iso_now(),
            "status": status, "rowsAffected": rows,
        })
    except LoaderError:
        print("  - ログの記録に失敗した")


def run(*, api: InternalApi, snapshot_dir: Path = DEFAULT_SNAPSHOT,
        limit: int = 200) -> Outcome:
    pending = api.get("predictions/pending", {"limit": str(limit)})
    if not isinstance(pending, dict):
        raise EvaluateError("照合対象の応答が不正")
    raw = pending.get("predictions")
    if raw is None:
        return Outcome()
    if not isinstance(raw, list):
        raise EvaluateError("照合対象の応答が不正")
    predictions = [p for p in raw if isinstance(p, dict)]
    if not predictions:
        return Outcome()
    dataset = load_snapshot(snapshot_dir)
    outcome = evaluate(predictions, dataset.table("games"))
    if outcome.results:
        send(api, outcome.results, summarize(outcome.results))
    return outcome


def _report(outcome: Outcome) -> None:
    counted = [r for r in outcome.results if r.counted]
    void = [r for r in outcome.results if not r.counted]
    print(f"照合: {len(outcome.results)}件"
          f"（母数に入る {len(counted)} / VOID {len(void)}）")
    if counted:
        print(f"  的中率 {_mean([float(r.is_correct or 0) for r in counted]):.3f}"
              f" / Brier {_mean([float(r.brier or 0.0) for r in counted]):.4f}")
        # **予想スコアの誤差は「全行にあるときだけ」出す**（4.12）。
        # 欠けていたら平均せず件数を出す — 黙って除いて平均すると、画面の
        # 「N試合中…」の隣に母数の違う数字が並ぶ
        mae = _score_mae_of(counted)
        if mae is None:
            missing = sum(1 for r in counted if r.score_mae is None)
            print(f"  予想スコアの誤差は出さない — {missing}件で予想スコアが"
                  f"保存されていない（母数が食い違う）")
        else:
            print(f"  予想スコアの誤差 {mae:.1f}点（1チームあたり）")
    for game_id, reason in outcome.skipped:
        print(f"  skip {game_id} {reason}")
    if outcome.skipped:
        print(f"  飛ばした: {len(outcome.skipped)}件")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="確定予測を実績と照合する")
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--dry-run", action="store_true", help="D1 には書かない")
    args = parser.parse_args(argv)

    try:
        api = InternalApi(
            os.environ.get("API_BASE_URL", ""),
            os.environ.get("INGEST_TOKEN", ""),
            dry_run=args.dry_run,
        )
        outcome = run(api=api, snapshot_dir=args.snapshot, limit=args.limit)
    except EvaluateError as error:
        print(f"中止: {error}", file=sys.stderr)
        return 1
    except LoaderError as error:
        print(f"中止: 内部APIとの通信に失敗した: {error}", file=sys.stderr)
        return 1
    _report(outcome)
    if not args.dry_run and outcome.results:
        _log(api, "SUCCESS", len(outcome.results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
