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
class FactorInput:
    """この予測に使った項目1つ（詳細設計 2.7.2 の `prediction_factors`）。

    **`ReasonInput` と別に持つ。** あちらは寄与の主張（`favors` / `contribution`）、
    こちらは「何を見たか」である。**`larger` は値が大きい側であって有利な側ではない。**
    """

    group_key: str
    label_ja: str
    value_text: str
    #: 向きを持たない列（片側の水準・両チーム共通・差が 0）は None
    larger: str | None = None


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
class ActualInput:
    """その試合の実績1人ぶん（詳細設計 3.3 の `playerActuals`）。

    **予測とは別の配列である**（v1.130。母集団が違う — 予測は `P(出場) >= 0.5`、
    実績は実際に出場した全員）。

    **成功数を持つ。** 予測側（`PlayerInput`）は率 × 試投数の導出値にするが、
    こちらは `player_game_stats` の取得値そのものである。

    **すべて NULL を取りうる。** 旧年度は `plus_minus` のキーが無く（4.4）、
    欠損を 0 に置換しない（規約5）。
    """

    player_id: str
    name: str
    club_id: str
    position: str | None = None
    started: bool | None = None
    minutes: float | None = None
    fg2m: int | None = None
    fg2a: int | None = None
    fg3m: int | None = None
    fg3a: int | None = None
    ftm: int | None = None
    fta: int | None = None
    oreb: int | None = None
    dreb: int | None = None
    ast: int | None = None
    tov: int | None = None
    stl: int | None = None
    blk: int | None = None
    pf: int | None = None
    fd: int | None = None
    plus_minus: int | None = None
    pts: int | None = None


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
class GameListRow:
    """一覧の1行（詳細設計 3.3 の `games[]`）。

    **タプルをやめて型にした**（v1.131）。照合の結果を持たせるためで、3要素の
    タプルにすると**どれが何かを呼び出し側が位置で覚える**ことになる。

    `evaluation` は**終了してもすぐには付かない**（freeze は毎時、照合は日次。
    基本設計 4.1）。**予測が無ければ照合も無い** — 照合は予測に対する判定である。
    """

    game: GameInput
    prediction: PredictionInput | None = None
    evaluation: EvaluationInput | None = None


@dataclass(frozen=True)
class GameListInput:
    """`today.json` / `schedule/<date>.json` の入力。"""

    game_date: str
    games: list[GameListRow] = field(default_factory=list)
    accuracy: AccuracyInput | None = None


@dataclass(frozen=True)
class GameDetailInput:
    """`games/<gameId>.json` の入力。"""

    game: GameInput
    prediction: PredictionInput | None = None
    reasons: list[ReasonInput] = field(default_factory=list)
    #: この予測に使った項目（詳細設計 2.7.2）。**21列すべて**
    factors: list[FactorInput] = field(default_factory=list)
    players: list[PlayerInput] = field(default_factory=list)
    #: その試合の実績（詳細設計 3.3）。**予測の有無に依存しない** — 予測が1本も
    #: 無い試合でも「この試合の記録」は出す
    actuals: list[ActualInput] = field(default_factory=list)
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
    #: **`/results`（引数なし）が既定で見る日**（詳細設計 3.7）。照合した試合の
    #: 最も新しい `game_date`。**この回に照合が無ければ前回の値を引き継ぐ**
    #: （`last_success_at` と同じ型。決めるのは writer 側）
    latest_result_date: str | None = None


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


def _actual_pct(made: int | None, attempted: int | None) -> float | None:
    """実績の率。**閾値を設けない**（要件 8.3 / 詳細設計 3.3）。

    `_pct` が閾値を課すのは**予測値**に対してであり、実績の `6 / 13` は丸めのない
    事実である。**試投数が0のときだけ None**（分母0の率は定義されない）。
    """
    if made is None or attempted is None or attempted <= 0:
        return None
    return made / attempted


def _sum(a: int | None, b: int | None) -> int | None:
    """片方が None なら合計も None。**0 として足さない**（規約5）。"""
    return None if a is None or b is None else a + b


def _actual(row: ActualInput) -> dict[str, Any]:
    """`playerActuals` の1人ぶん（詳細設計 3.3）。

    **`pts` は取得値をそのまま出す。** 恒等式との一致は取り込みが検証している（1.3）。
    **`plusMinus` は実績のみ**（要件 6.8.3）。
    """
    fgm = _sum(row.fg2m, row.fg3m)
    fga = _sum(row.fg2a, row.fg3a)
    ts_denominator = (
        None if fga is None or row.fta is None
        else 2 * (fga + TS_FTA_COEFFICIENT * row.fta)
    )
    return {
        "playerId": row.player_id,
        "name": row.name,
        "position": row.position,
        "clubId": row.club_id,
        "started": row.started,
        "summary": {
            "min": row.minutes,
            "pts": row.pts,
            "reb": _sum(row.oreb, row.dreb),
            "ast": row.ast,
        },
        "box": {
            "fg": {"m": fgm, "a": fga, "pct": _actual_pct(fgm, fga)},
            "fg2": {"m": row.fg2m, "a": row.fg2a, "pct": _actual_pct(row.fg2m, row.fg2a)},
            "fg3": {"m": row.fg3m, "a": row.fg3a, "pct": _actual_pct(row.fg3m, row.fg3a)},
            "ft": {"m": row.ftm, "a": row.fta, "pct": _actual_pct(row.ftm, row.fta)},
            "oreb": row.oreb,
            "dreb": row.dreb,
            "ast": row.ast,
            "tov": row.tov,
            "stl": row.stl,
            "blk": row.blk,
            "pf": row.pf,
            "fd": row.fd,
            "plusMinus": row.plus_minus,
            "efgPct": (
                None
                if fgm is None or row.fg3m is None or fga is None or fga <= 0
                else (fgm + 0.5 * row.fg3m) / fga
            ),
            "tsPct": (
                None if ts_denominator is None or ts_denominator <= 0 or row.pts is None
                else row.pts / ts_denominator
            ),
        },
    }


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
    for row in source.games:
        game, prediction = row.game, row.prediction
        finished = game.status == "FINISHED"
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
                # **終了した試合は実績も出す**（v1.131。運営者の指摘）。
                # **`status` が FINISHED でなければ出さない** — 途中経過が入って
                # いても出さないのは詳細と同じ関門である
                "homeScore": game.home_score if finished else None,
                "awayScore": game.away_score if finished else None,
                # **照合していなければ null。** 0 や false で埋めない。
                # **予測が無ければ照合も無い** — ここでも落とす（呼び出し側の
                # 取りこぼしで、判定する対象の無い判定が出ないようにする）
                "evaluation": (
                    None
                    if row.evaluation is None or prediction is None
                    else {
                        "isCorrect": row.evaluation.is_correct,
                        "scoreError": row.evaluation.score_error,
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
        # **実績は予測の有無に依存しない**（詳細設計 3.3）。予測が1本も無い試合でも
        # 「この試合の記録」は出す — 依存させると画面に何も出ない
        "playerActuals": [_actual(row) for row in source.actuals],
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
        # **使った項目の一覧**（詳細設計 2.7.2）。`reasons` とは別の問いへの答え
        # であり、**有利不利を主張しない**（`larger` は値が大きい側）
        "factors": [
            {
                "group": factor.group_key,
                "label": factor.label_ja,
                "value": factor.value_text,
                "larger": factor.larger,
            }
            for factor in source.factors
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
            "latestResultDate": source.latest_result_date,
            "modelVersions": list(source.model_versions),
        },
        source.generated_at,
    )
