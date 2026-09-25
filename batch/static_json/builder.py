"""静的JSON の組み立て（詳細設計 3.7）。

**キーの位置を試合の状態で動かさない。** 試合前後で変わるのは各フィールドの中身で
あって、キーの位置ではない（詳細設計 3.3）。状態でキーが動くと、読む側が状態ごとに
別のパスを持つことになり、片方だけ壊れる。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

# 率を出す試投数の閾値（詳細設計 3.3）。**下回るときは率を出さず分数だけを出す。**
# 1試合の試投数は6本程度で、実績としての FG% は離散値しか取りえない。そこに
# 「47.3%」とだけ出すのは存在しない精度を主張することになる（要件 6.8.2）。
PCT_THRESHOLD = {"fg": 4, "fg2": 3, "fg3": 3, "ft": 3}

# 寄与の強さの段階数（詳細設計 2.7）。**SHAP の生値も符号付きの値も出さない。**
STRENGTH_STEPS = 4

# TS% の分母に使う係数。NBA 由来であり、**妥当性は P0-7 で再推定する**（詳細設計 1.3）
TS_FTA_COEFFICIENT = 0.44


@dataclass(frozen=True)
class ClubInput:
    """クラブの表示。**表示名は年度断面（`club_seasons`）の値を渡す。**

    `clubs.name` は現在の表示名であり、過去試合に出すと遡って変わる（詳細設計 1.2）。
    """

    club_id: str
    slug: str
    name: str | None
    short_name: str | None


@dataclass(frozen=True)
class PredictionInput:
    home_win_prob: float
    pred_home_score: float | None
    pred_away_score: float | None
    is_provisional: bool
    is_final: bool
    model_version: str
    # 消化試合5未満の判定。**推測で False を入れない** — 集計がなければ None
    is_early_season: bool | None = None


@dataclass(frozen=True)
class ReasonInput:
    group_key: str
    label_ja: str
    value_text: str
    favors: str
    # ログオッズ空間の値。**そのままは出さず段階値に畳む**（詳細設計 2.7）
    contribution: float


@dataclass(frozen=True)
class PlayerInput:
    """整合化後の選手予測。**成功数は持たない**（率 × 試投数の導出値。詳細設計 1.5）。"""

    player_id: str
    name: str
    club_id: str
    position: str | None
    avail_prob: float
    minutes: float
    fg2a: float
    fg3a: float
    fta: float
    fg2_pct: float
    fg3_pct: float
    ft_pct: float
    oreb: float
    dreb: float
    ast: float
    tov: float
    stl: float
    blk: float
    pf: float
    fd: float
    # 誤差の目安は主要4項目だけ（要件 6.8.6）。全項目に出すと画面が埋まる
    err_minutes: float | None = None
    err_pts: float | None = None
    err_reb: float | None = None
    err_ast: float | None = None


@dataclass(frozen=True)
class EvaluationInput:
    is_correct: bool | None
    score_error: float | None
    outcome: str
    bucket: str | None = None
    bucket_n: int | None = None
    bucket_rate: float | None = None


@dataclass(frozen=True)
class AccuracyInput:
    accuracy: float
    brier: float
    n: int


@dataclass(frozen=True)
class GameInput:
    """一覧・詳細に共通する試合の事実。"""

    game_id: str
    tipoff_at: str
    status: str
    competition: str
    home: ClubInput
    away: ClubInput
    league: str | None = None
    venue_name: str | None = None
    is_primary_venue: bool = True
    home_score: int | None = None
    away_score: int | None = None


@dataclass(frozen=True)
class GameListInput:
    """`today.json` / `schedule/<date>.json` の入力。"""

    game_date: str
    games: list[tuple[GameInput, PredictionInput | None]] = field(default_factory=list)
    accuracy: AccuracyInput | None = None


@dataclass(frozen=True)
class GameDetailInput:
    """`games/<gameId>.json` の入力。"""

    game: GameInput
    prediction: PredictionInput | None = None
    reasons: list[ReasonInput] = field(default_factory=list)
    players: list[PlayerInput] = field(default_factory=list)
    evaluation: EvaluationInput | None = None
    model_accuracy: AccuracyInput | None = None


@dataclass(frozen=True)
class MetaInput:
    """`meta.json` の入力（詳細設計 3.7）。"""

    generated_at: str
    data_as_of: str | None
    last_run_status: str
    # `SUCCESS` 以外の回では前回の値を引き継ぐ。決めるのは writer 側
    last_success_at: str | None
    model_versions: list[str] = field(default_factory=list)


def _envelope(data: dict[str, Any], generated_at: str) -> dict[str, Any]:
    """`{ "data": ..., "meta": ... }`（詳細設計 3.1）。

    **`meta` に `generatedAt` 以外を足さない。** 公開APIの `meta` と同じ形に保つため
    で、鮮度やジョブの状態は `meta.json` が持つ（詳細設計 3.7）。
    """
    return {"data": data, "meta": {"generatedAt": generated_at}}


def _club(club: ClubInput) -> dict[str, Any]:
    return {
        "clubId": club.club_id,
        "slug": club.slug,
        "name": club.name,
        "shortName": club.short_name,
    }


def _pct(made: float, attempted: float, threshold: int) -> float | None:
    """試投数が閾値未満なら率を出さない（要件 6.8.2）。"""
    return made / attempted if attempted >= threshold else None


def _strength(contribution: float, largest: float) -> int:
    if largest <= 0:
        return 1
    ratio = abs(contribution) / largest
    return max(1, math.ceil(ratio * STRENGTH_STEPS))


def _box(player: PlayerInput) -> dict[str, Any]:
    """導出項目を計算する。**独立に予測した値を受け取らない**（要件 6.8.2）。

    恒等式（`FGM = 2FGM + 3FGM`、`PTS = 2FGM×2 + 3FGM×3 + FTM`）はここで導出する
    ことで構造的に成立する。
    """
    fg2m = player.fg2_pct * player.fg2a
    fg3m = player.fg3_pct * player.fg3a
    ftm = player.ft_pct * player.fta
    fgm = fg2m + fg3m
    fga = player.fg2a + player.fg3a
    pts = fg2m * 2 + fg3m * 3 + ftm
    reb = player.oreb + player.dreb
    ts_denominator = 2 * (fga + TS_FTA_COEFFICIENT * player.fta)
    return {
        "summary": {"min": player.minutes, "pts": pts, "reb": reb, "ast": player.ast},
        "error": {
            "min": player.err_minutes,
            "pts": player.err_pts,
            "reb": player.err_reb,
            "ast": player.err_ast,
        },
        "box": {
            "fg": {"m": fgm, "a": fga, "pct": _pct(fgm, fga, PCT_THRESHOLD["fg"])},
            "fg2": {
                "m": fg2m,
                "a": player.fg2a,
                "pct": _pct(fg2m, player.fg2a, PCT_THRESHOLD["fg2"]),
            },
            "fg3": {
                "m": fg3m,
                "a": player.fg3a,
                "pct": _pct(fg3m, player.fg3a, PCT_THRESHOLD["fg3"]),
            },
            "ft": {
                "m": ftm,
                "a": player.fta,
                "pct": _pct(ftm, player.fta, PCT_THRESHOLD["ft"]),
            },
            "oreb": player.oreb,
            "dreb": player.dreb,
            "ast": player.ast,
            "tov": player.tov,
            "stl": player.stl,
            "blk": player.blk,
            "pf": player.pf,
            "fd": player.fd,
            "efgPct": _pct(fgm + 0.5 * fg3m, fga, PCT_THRESHOLD["fg"]),
            "tsPct": (
                pts / ts_denominator
                if ts_denominator > 0 and fga >= PCT_THRESHOLD["fg"]
                else None
            ),
        },
    }


def _accuracy(value: AccuracyInput | None) -> dict[str, Any] | None:
    # **母数を必ず併記する**（要件 8.3）。率だけを出さない
    if value is None:
        return None
    return {"accuracy": value.accuracy, "brier": value.brier, "n": value.n}


def build_game_list(source: GameListInput, generated_at: str) -> dict[str, Any]:
    """`GET /games?date=` と同じ形を作る（詳細設計 3.3 / 3.7）。"""
    games: list[dict[str, Any]] = []
    for game, prediction in source.games:
        games.append(
            {
                "gameId": game.game_id,
                # **UTC のまま出す。** JST への変換は画面で行う（CLAUDE.md 時刻の扱い）
                "tipoffAt": game.tipoff_at,
                "status": game.status,
                "competition": game.competition,
                "home": _club(game.home),
                "away": _club(game.away),
                # 予測がまだない試合は null。**試合ごと省略しない** — 日程に載って
                # いるのに予測がない状態は実在し、画面は空状態を出す（要件 8.5）
                "prediction": (
                    None
                    if prediction is None
                    else {
                        "homeWinProb": prediction.home_win_prob,
                        "predHomeScore": prediction.pred_home_score,
                        "predAwayScore": prediction.pred_away_score,
                        "isProvisional": prediction.is_provisional,
                        "isFinal": prediction.is_final,
                        "isEarlySeason": prediction.is_early_season,
                        "modelVersion": prediction.model_version,
                    }
                ),
            }
        )
    return _envelope(
        {
            # **自分の日付を必ず持つ**（詳細設計 3.7）。`today.json` は 06:00 JST に
            # 書き出されるため、00:00〜06:00 JST の間は中身が前日である
            "gameDate": source.game_date,
            "games": games,
            "accuracy": _accuracy(source.accuracy),
        },
        generated_at,
    )


def build_game_detail(source: GameDetailInput, generated_at: str) -> dict[str, Any]:
    """`GET /games/:gameId` と同じ形を作る（詳細設計 3.3 / 3.7）。"""
    game = source.game
    finished = game.status == "FINISHED"
    data: dict[str, Any] = {
        "game": {
            "gameId": game.game_id,
            "tipoffAt": game.tipoff_at,
            "league": game.league,
            "competition": game.competition,
            "status": game.status,
            "home": _club(game.home),
            "away": _club(game.away),
            "venue": (
                None
                if game.venue_name is None
                else {"name": game.venue_name, "isPrimary": game.is_primary_venue}
            ),
            "homeScore": game.home_score if finished else None,
            "awayScore": game.away_score if finished else None,
        },
        "prediction": None,
        "evaluation": None,
        "playerPredictions": [],
        # 直近成績は未実装（公開API 側も null を返す）。**キーは落とさない**
        "recentForm": None,
        "modelAccuracy": None,
    }

    prediction = source.prediction
    if prediction is None:
        return _envelope(data, generated_at)

    largest = max((abs(r.contribution) for r in source.reasons), default=0.0)
    data["prediction"] = {
        "homeWinProb": prediction.home_win_prob,
        "predHomeScore": prediction.pred_home_score,
        "predAwayScore": prediction.pred_away_score,
        "isProvisional": prediction.is_provisional,
        "isFinal": prediction.is_final,
        "modelVersion": prediction.model_version,
        "reasons": [
            {
                "group": reason.group_key,
                # 生の特徴量名を出さない（要件 6.9）
                "label": reason.label_ja,
                "value": reason.value_text,
                "favors": reason.favors,
                "strength": _strength(reason.contribution, largest),
            }
            for reason in source.reasons
        ],
    }
    # `avail_prob < 0.5` の選手は出さない（要件 6.8.4）。**ここでも落とす** —
    # 呼び出し側の絞り込み漏れで欠場濃厚な選手が表示されないようにする
    data["playerPredictions"] = [
        {
            "playerId": player.player_id,
            "name": player.name,
            "position": player.position,
            "clubId": player.club_id,
            "availProb": player.avail_prob,
            **_box(player),
        }
        for player in source.players
        if player.avail_prob >= 0.5
    ]
    data["modelAccuracy"] = (
        None
        if source.model_accuracy is None
        else {
            "version": prediction.model_version,
            "accuracy": source.model_accuracy.accuracy,
            "brier": source.model_accuracy.brier,
            "n": source.model_accuracy.n,
        }
    )

    evaluation = source.evaluation
    if evaluation is not None:
        data["evaluation"] = {
            "isCorrect": evaluation.is_correct,
            "scoreError": evaluation.score_error,
            "outcome": evaluation.outcome,
            # **外れた試合でも返す。** その確率帯の通算的中率を併記して、較正が
            # 取れていること自体を信頼の材料にする（基本設計 5.2）
            "bucketContext": (
                None
                if evaluation.bucket is None or evaluation.bucket_n is None
                else {
                    "bucket": evaluation.bucket,
                    "n": evaluation.bucket_n,
                    "correct": round((evaluation.bucket_rate or 0.0) * evaluation.bucket_n),
                    "rate": evaluation.bucket_rate,
                }
            ),
        }

    return _envelope(data, generated_at)


def build_meta(source: MetaInput) -> dict[str, Any]:
    """`meta.json`（詳細設計 3.7）。

    **`stale` を真偽値で入れない。** バッチは自分が書いた時点しか知らず、「今」から
    24時間経ったかを判定できない。判定はクライアントが `lastSuccessAt` と現在時刻で
    行う（バッチが `stale: false` と書いた瞬間に古くなる）。
    """
    return _envelope(
        {
            "generatedAt": source.generated_at,
            "dataAsOf": source.data_as_of,
            "lastRunStatus": source.last_run_status,
            "lastSuccessAt": source.last_success_at,
            "modelVersions": list(source.model_versions),
        },
        source.generated_at,
    )
