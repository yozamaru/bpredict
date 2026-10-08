"""解析結果を `/internal/*` の本文へ変換する。**取得もDB書き込みもしない純粋な変換。**

対応表の正本は詳細設計 4.4、本文の形は同 3.4。ここでは次を決める。

- 公式 `TeamID` を `club_source_ids` で内部 `club_id` に解決する（旧IDのまま保存しない）
- `series_game_no`（同一カード連戦の何戦目か）をシーズンの日程から導出する
- `spectator_restricted` を取り込み時に判定して列に書く（特徴量生成で計算し直さない）
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from batch.model.predict import Prediction
from batch.parser.models import BoxScore
from batch.parser.schedule_parser import ScheduleGame

#: 観客制限期間。入場者数が非公開の試合を取りこぼさないため**期間指定で強制的に 1**
#: とする（詳細設計 1.3）。`attendance / capacity < 0.2` の判定より優先する。
SPECTATOR_RESTRICTED_SEASONS = frozenset({"2020-21", "2021-22"})

#: 収容人数が未投入のあいだ動員率を判定できない。**NULL は「判定不能」**であり、
#: Elo では通常のホームアドバンテージを使う（詳細設計 2.5）。
RESTRICTED_RATIO = 0.2


class PayloadError(ValueError):
    """解決できない参照がある（クラブ・シーズンの対応が取れない）。"""


@dataclass(frozen=True)
class SeasonRef:
    season_id: str
    label: str
    league: str


def resolve_club(source_id: str, club_ids: Mapping[str, str]) -> str:
    """公式 `TeamID` → 内部 `club_id`。**旧IDのまま保存しない**（詳細設計 1.2）。"""
    club_id = club_ids.get(source_id)
    if not club_id:
        raise PayloadError(f"公式IDを club_id に解決できない: {source_id}")
    return club_id


def series_numbers(games: Sequence[tuple[str, str, str, str]]) -> dict[str, int]:
    """同一カード連戦の何戦目かを日程から導出する。

    引数は `(game_id, game_date, home_source_id, away_source_id)` の並び。
    **前戦が前日までなら連戦の続きと数える**（B.LEAGUE は土日2連戦が基本編成）。
    順序に依存するため、呼ぶ側がシーズン全体を日付順で渡す。
    """
    numbers: dict[str, int] = {}
    last: dict[frozenset[str], tuple[date, int]] = {}
    for game_id, game_date, home, away in sorted(games, key=lambda row: (row[1], row[0])):
        day = date.fromisoformat(game_date)
        pair = frozenset({home, away})
        previous = last.get(pair)
        continued = previous is not None and (day - previous[0]).days <= 1
        number = previous[1] + 1 if continued and previous is not None else 1
        numbers[game_id] = number
        last[pair] = (day, number)
    return numbers


def spectator_restricted(season_label: str, attendance: int | None) -> int | None:
    """取り込み時に判定して列に書く（詳細設計 1.3）。

    収容人数が未投入のため、通常期は判定不能（NULL）になる。観客制限期間は
    **期間指定で強制的に 1** とするので、コロナ期の Elo には影響しない。
    """
    if season_label in SPECTATOR_RESTRICTED_SEASONS:
        return 1
    if attendance is None:
        return None
    return None  # capacity が未投入のあいだは判定できない


def games_payload(
    box: BoxScore,
    *,
    season: SeasonRef,
    club_ids: Mapping[str, str],
    short_names: Mapping[str, str],
    series_game_no: int | None,
    source_url: str,
    fetched_at: str,
) -> dict[str, object]:
    """`POST /internal/games` の本文。FK 順は API 側が保証する（詳細設計 3.4）。"""
    game = box.game
    home = resolve_club(game.home_club_id, club_ids)
    away = resolve_club(game.away_club_id, club_ids)

    payload: dict[str, object] = {
        "games": [
            {
                "id": game.id,
                "seasonId": season.season_id,
                "league": season.league,
                "competition": game.competition,
                "gameDate": game.game_date,
                "tipoffAt": game.tipoff_at,
                "finishedAt": game.finished_at,
                "finishedAtIsEstimated": game.finished_at_is_estimated,
                "homeClubId": home,
                "awayClubId": away,
                "venueId": game.venue_id,
                # その試合時点の会場名。`venue_revisions.name` の唯一の入力（詳細設計 1.2）。
                # `venues` の upsert は `name` を更新しないため、ここに残さないと履歴が作れない
                "venueNameAtGame": game.venue_name,
                "seriesGameNo": series_game_no,
                "status": game.status,
                "homeScore": game.home_score,
                "awayScore": game.away_score,
                "attendance": game.attendance,
                "spectatorRestricted": spectator_restricted(season.label, game.attendance),
                "sourceUrl": source_url,
                "fetchedAt": fetched_at,
            }
        ],
        "teamGames": [
            {
                "gameId": game.id,
                "clubId": club,
                "opponentId": opponent,
                "seasonId": season.season_id,
                "gameDate": game.game_date,
                "finishedAt": game.finished_at,
                "isHome": is_home,
                "competition": game.competition,
                "result": 1 if score > opponent_score else 0,
                "margin": score - opponent_score,
            }
            for club, opponent, is_home, score, opponent_score in (
                (home, away, 1, game.home_score, game.away_score),
                (away, home, 0, game.away_score, game.home_score),
            )
        ],
        "players": [
            {"id": player.player_id, "name": player.name} for player in box.players
        ],
    }

    if game.venue_id is not None:
        payload["venues"] = [{"id": game.venue_id, "name": game.venue_name or game.venue_id}]
        payload["venueSourceKeys"] = [{"sourceCode": game.venue_id, "venueId": game.venue_id}]

    club_seasons = []
    for source_id, club_id, name in (
        (game.home_club_id, home, game.home_name),
        (game.away_club_id, away, game.away_name),
    ):
        short = short_names.get(source_id)
        if short is None:
            # 短縮名は日程ページの選択肢から取る。取れないまま推測で埋めない
            continue
        club_seasons.append(
            {
                "clubId": club_id,
                "seasonId": season.season_id,
                "name": name,
                "shortName": short,
                "league": season.league,
            }
        )
    if club_seasons:
        payload["clubSeasons"] = club_seasons
    return payload


def stats_payload(box: BoxScore, *, club_ids: Mapping[str, str], fetched_at: str) -> dict[str, object]:
    """`POST /internal/stats` の本文。対応表にある列だけを送る（詳細設計 4.4）。"""
    game = box.game
    team_stats = [
        {
            "gameId": team.game_id,
            "clubId": resolve_club(team.club_id, club_ids)
            if team.club_id in club_ids
            else team.club_id,
            "gameDate": game.game_date,
            "isHome": team.is_home,
            "possessions": team.possessions,
            "fetchedAt": fetched_at,
            **{name: getattr(team.stats, name) for name in _COUNT_FIELDS},
        }
        for team in box.teams
    ]
    player_stats = [
        {
            "gameId": player.game_id,
            "playerId": player.player_id,
            "clubId": player.club_id,
            "gameDate": game.game_date,
            "started": player.started,
            "minutes": player.minutes,
            "plusMinus": player.plus_minus,
            "fetchedAt": fetched_at,
            **{name: getattr(player.stats, name) for name in _COUNT_FIELDS},
        }
        for player in box.players
    ]
    return {"teamGameStats": team_stats, "playerGameStats": player_stats}


_COUNT_FIELDS = (
    "pts", "fg2m", "fg2a", "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb",
    "ast", "tov", "stl", "blk", "pf", "fd",
)


def upcoming_games_payload(
    games: Sequence[ScheduleGame],
    *,
    season: SeasonRef,
    club_ids: Mapping[str, str],
    series_game_no: Mapping[str, int],
    fetched_at: str,
) -> dict[str, object]:
    """未実施の試合の `POST /internal/games` の本文（詳細設計 4.2 のステップ1b）。

    **`games_payload` と分けてある。** あちらはボックススコア（試合後）を入力に取る。
    未実施の試合には**スコアも会場IDも無い**ため、同じ関数では組めない。

    **会場を送らない。** 日程ページは会場名の文字しか持たず、公式の `StadiumCD` は
    ボックススコアにしかない（詳細設計 2.2）。`venueId` と `venueNameAtGame` は
    NULL のままにし、**試合後にステップ1（ボックススコアの取り込み）が埋める**。

    **`clubSeasons` を送らない。** `ScheduleGame` のクラブ名は**略称のことがある**
    （2020-21 の `千葉J` / `横浜BC`。詳細設計 4.4）。`club_seasons.name` は
    ボックススコアの `TeamNameJ`（その試合時点の正式名称）が出典であり、
    **ここで略称を入れると正式名称を上書きする**。

    **`result` と `margin` は NULL。** DDL は「NULL = 未実施」と定めている（1.3）。
    0 を入れると「引き分け」の意味になる。
    """
    rows: list[dict[str, object]] = []
    team_rows: list[dict[str, object]] = []
    for game in games:
        home = resolve_club(game.home_source_id, club_ids)
        away = resolve_club(game.away_source_id, club_ids)
        rows.append(
            {
                "id": game.game_id,
                "seasonId": season.season_id,
                "league": season.league,
                "competition": game.competition,
                "gameDate": game.game_date,
                "tipoffAt": game.tipoff_at,
                "finishedAt": None,
                "finishedAtIsEstimated": 0,
                "homeClubId": home,
                "awayClubId": away,
                "venueId": None,
                "venueNameAtGame": None,
                "seriesGameNo": series_game_no.get(game.game_id),
                "status": game.status,
                "homeScore": None,
                "awayScore": None,
                "attendance": None,
                "spectatorRestricted": spectator_restricted(season.label, None),
                "sourceUrl": game.source_url,
                "fetchedAt": fetched_at,
            }
        )
        team_rows.extend(
            {
                "gameId": game.game_id,
                "clubId": club,
                "opponentId": opponent,
                "seasonId": season.season_id,
                "gameDate": game.game_date,
                "finishedAt": None,
                "isHome": is_home,
                "competition": game.competition,
                "result": None,
                "margin": None,
            }
            for club, opponent, is_home in ((home, away, 1), (away, home, 0))
        )
    return {"games": rows, "teamGames": team_rows}


def prediction_payload(
    *,
    game_id: str,
    season_id: str,
    run_id: str,
    predicted_at: str,
    as_of: str,
    data_as_of: str,
    prediction: Prediction,
    features: Mapping[str, float],
    model_versions: Mapping[str, str],
    reasons: Sequence[Mapping[str, object]] = (),
    factors: Sequence[Mapping[str, object]] = (),
    team_targets: Sequence[Mapping[str, object]] = (),
    player_predictions: Sequence[Mapping[str, object]] = (),
    rate_versions: Mapping[tuple[str, str], str] | None = None,
) -> dict[str, object]:
    """`POST /internal/predictions` の本文（詳細設計 4.2 のステップ4）。

    **1リクエストに1試合である**（3.4 の口がそう作られている）。

    **`teamTargets` は「2件か0件」でなければ Zod が拒否する** — 1件は片側だけ
    整合化した状態であり、原理的に誤りである。**個人スタッツも両チーム揃って
    初めて送る**（4.2）— 画面は「ホームだけフルボックススコアがある」状態を
    想定していない。

    **`reasons` は現在の21列では2件である**（`VENUE` に該当列がなく、`PLAYER` の
    3列は定数で寄与が厳密に 0。2.7.1）。**したがって受け入れ基準 A-01 の
    「根拠3件以上」は依然として満たさない。** 満たさないことを承知のうえで通す。

    **`factors` は21件である**（2.7.2）。`reasons` の不足を埋めるものではない —
    あちらは寄与の主張、こちらは「使った項目の事実」であり、**A-01 の判定は
    `reasons` に対して行う**。

    **`isProvisional` は常に 1。** エントリー情報を取得していない（`game_entries`
    は0行）。確定するのは `gameday_update` が入ってからである。
    """
    if not model_versions:
        raise PayloadError("使ったモデルの版が空である")
    if len(team_targets) == 1:
        raise PayloadError("teamTargets は2件か0件（片側だけは原理的に誤り）")
    if player_predictions and not team_targets:
        raise PayloadError("チーム目標なしに個人スタッツを送れない")
    return {
        "gameId": game_id,
        "seasonId": season_id,
        # **代表バージョンは WINNER である**（1.6）。全体は modelBundle が持つ
        "modelVersion": model_versions["WINNER"],
        "runId": run_id,
        "predictedAt": predicted_at,
        "asOf": as_of,
        "dataAsOf": data_as_of,
        "homeWinProb": prediction.home_win_prob,
        "predMargin": prediction.margin,
        "predTotal": prediction.total,
        "predHomeScore": prediction.home_score,
        "predAwayScore": prediction.away_score,
        "isProvisional": 1,
        # **丸めない。** 画面に出すときに整数へ丸める（要件 8.3）
        "featureSnapshot": json.dumps(
            {k: float(v) for k, v in features.items()}, ensure_ascii=False),
        "modelBundle": [
            {"modelType": model_type, "target": "", "modelVersion": version}
            for model_type, version in model_versions.items()
        ] + [
            # **30本は `target` を持つ**（4.5.1）。`prediction_model_bundle` の
            # 主キーは `(prediction_id, model_type, target)` であり、14本を
            # `target` なしで入れると1本目以外が主キー違反で落ちる
            {"modelType": model_type, "target": target, "modelVersion": version}
            for (model_type, target), version in sorted(
                (rate_versions or {}).items())
        ],
        **({"reasons": [dict(r) for r in reasons]} if reasons else {}),
        # **使った項目の一覧**（詳細設計 2.7.2）。`reasons` とは別物で、
        # 寄与ではなく「何を見たか」を21列すべて並べる
        **({"factors": [dict(r) for r in factors]} if factors else {}),
        **({"teamTargets": [dict(r) for r in team_targets]} if team_targets else {}),
        **(
            {"playerPredictions": [dict(r) for r in player_predictions]}
            if player_predictions else {}
        ),
    }


#: 整合化の14項目 → `prediction_team_targets` の列（詳細設計 1.5）。
#: **キーを2箇所に書かない** — 整合化の集合は `batch/model/reconcile.py` が正で、
#: ここは DB の列名への写しだけを持つ。
TEAM_TARGET_COLUMNS: Mapping[str, str] = {
    "fg2a": "tgtFg2a", "fg3a": "tgtFg3a", "fta": "tgtFta",
    "fg2_pct": "tgtFg2Pct", "fg3_pct": "tgtFg3Pct", "ft_pct": "tgtFtPct",
    "oreb": "tgtOreb", "dreb": "tgtDreb", "ast": "tgtAst", "tov": "tgtTov",
    "stl": "tgtStl", "blk": "tgtBlk", "pf": "tgtPf", "fd": "tgtFd",
}

#: 同じ14項目 → `player_predictions` の列。
PLAYER_COLUMNS: Mapping[str, str] = {
    "fg2a": "predFg2a", "fg3a": "predFg3a", "fta": "predFta",
    "fg2_pct": "predFg2Pct", "fg3_pct": "predFg3Pct", "ft_pct": "predFtPct",
    "oreb": "predOreb", "dreb": "predDreb", "ast": "predAst", "tov": "predTov",
    "stl": "predStl", "blk": "predBlk", "pf": "predPf", "fd": "predFd",
}


def team_target_payload(
    *, club_id: str, is_home: bool, targets: Mapping[str, float],
) -> dict[str, object]:
    """`prediction_team_targets` の1行（整合化の前段を通した後の値）。

    **成功率は `[0, 1]` を出ない**（ロジット空間でシフトしたため）。それでも
    DB の CHECK 制約があるため、**ここで丸めない** — 出たら Zod が 400 を返し、
    上流の欠陥として気づけるようにする（無言でクリップしない。2.4）。
    """
    missing = sorted(k for k in TEAM_TARGET_COLUMNS if k not in targets)
    if missing:
        raise PayloadError(f"チーム目標に項目が足りない: {missing}")
    row: dict[str, object] = {"clubId": club_id, "isHome": 1 if is_home else 0}
    for key, column in TEAM_TARGET_COLUMNS.items():
        row[column] = float(targets[key])
    return row


def player_prediction_payload(
    *, player_id: str, club_id: str, avail_prob: float, minutes: float,
    counts: Mapping[str, float], pcts: Mapping[str, float],
) -> dict[str, object]:
    """`player_predictions` の1行（整合化を通した後の値）。

    **`err_*` は送らない。** 要件 6.8.6 の `N`（直近N試合）が未定義であり、
    過去の個人予測と実績の対比が1件もない（2.3.1）。**0 を入れない** —
    0 は「誤差がない」という意味を持ってしまう。
    """
    row: dict[str, object] = {
        "playerId": player_id, "clubId": club_id,
        "availProb": float(avail_prob), "predMinutes": float(minutes),
    }
    for key, column in PLAYER_COLUMNS.items():
        source = pcts if key.endswith("_pct") else counts
        if key not in source:
            raise PayloadError(f"選手予測に項目が足りない: {key}")
        row[column] = float(source[key])
    return row


#: `POST /internal/games` の本文のキー → スナップショットの列（詳細設計 4.2 のステップ2）。
#: **本文から作る。** 別に組むと、片方だけ直したときに D1 とスナップショットが
#: 食い違う（基本設計 2.2「D1 を更新するジョブは同じ値をスナップショットにも書く」）。
_SNAKE = re.compile(r"(?<!^)(?=[A-Z])")

#: DDL の DEFAULT を写す列（詳細設計 1.3）。**D1 が入れる値をこちらでも入れる。**
#: `created_at` / `updated_at` は写さない — **D1 が自分の時計と書式で入れる**ため、
#: こちらの値を書くと「D1 が記録していない時刻」を主張することになる。
#: 次の全体再構築（8.1 手順6）で D1 の値に揃う。
SNAPSHOT_DEFAULTS: dict[str, object] = {
    "is_primary_venue": 1,
    "result_revision": 0,
    "rescheduled_to": None,
}


def rosters_payload(
    *,
    players: Sequence[Mapping[str, object]],
    player_seasons: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """`POST /internal/rosters` の本文（詳細設計 3.4 / 4.13）。

    **出せないものはキーを送らない。** `height_cm` / `roster_type` /
    `joined_on` / `left_on` はいずれも出典が無く（要件 5.3）、口の側で
    「NULL で上書きしない」保護が掛かっている。

    **片方だけでも送れる。** `players` を先に入れる FK の順序は口の側が持つ。
    """
    if not players and not player_seasons:
        raise PayloadError("選手も所属断面も空である")
    body: dict[str, object] = {}
    if players:
        body["players"] = [dict(p) for p in players]
    if player_seasons:
        body["playerSeasons"] = [dict(s) for s in player_seasons]
    return body


def _snake(key: str) -> str:
    return _SNAKE.sub("_", key).lower()


#: 本文の配列 → スナップショットのテーブル（詳細設計 4.2 のステップ1 / 2）。
#:
#: **`POST /internal/games` と `POST /internal/stats` の両方を受ける。** ステップ1b が
#: 触るのは `games` と `team_games` だけだが、**ステップ1 はマスタも書く** — 写す範囲を
#: 広げないと `club_seasons` が D1 にだけ入る（基本設計 2.2 が座標140件で踏んだ形）。
SNAPSHOT_TABLES: dict[str, str] = {
    "games": "games",
    "teamGames": "team_games",
    "players": "players",
    "venues": "venues",
    "clubSeasons": "club_seasons",
    "teamGameStats": "team_game_stats",
    "playerGameStats": "player_game_stats",
    # **`POST /internal/rosters` も受ける**（詳細設計 4.13）。書かないと第1段から
    # 見えない — 入力はスナップショットだけである（絶対ルール3）
    "playerSeasons": "player_seasons",
}

#: **スナップショットが持たない配列**（基本設計 2.2 のファイル一覧）。
#:
#: 黙って落とさず、**名前と理由をここに書く** — 「無いテーブルは飛ばす」という
#: 一般の規則にすると、本当に写し忘れたテーブルも静かに通る。
NOT_IN_SNAPSHOT: dict[str, str] = {
    # 公式ID → 内部ID の対応表。**取り込みが使うもので、学習入力ではない**。
    # 特徴量・学習・推論のどれも読まないため、スナップショットに置かない
    "venueSourceKeys": "名寄せの対応表であり、学習入力ではない",
}


def snapshot_rows(*payloads: Mapping[str, object]) -> dict[str, list[dict[str, object]]]:
    """内部APIの本文を、スナップショットの行に写す。

    **キーの変換だけを行う**（camelCase → snake_case）。値は触らない —
    「D1 に送ったのと同じ値」であることが要点である（詳細設計 4.2 のステップ2）。

    複数の本文を渡せる（ステップ1 は `games` と `stats` の2つを送る）。本文に無い
    配列は返さない。`games` には DDL の DEFAULT を持つ列を足す（`SNAPSHOT_DEFAULTS`）。
    """
    out: dict[str, list[dict[str, object]]] = {}
    for payload in payloads:
        unknown = sorted(
            set(payload) - set(SNAPSHOT_TABLES) - set(NOT_IN_SNAPSHOT))
        if unknown:
            # **黙って落とさない。** 本文に配列を足したときに、スナップショットへ
            # 写す/写さないの判断をここで必ず迫る
            raise PayloadError(f"スナップショットへの写し方が未定の配列: {unknown}")
        for key, table in SNAPSHOT_TABLES.items():
            rows = payload.get(key)
            if not isinstance(rows, list) or not rows:
                continue
            written = out.setdefault(table, [])
            for row in rows:
                if not isinstance(row, dict):
                    raise PayloadError("本文の行が辞書でない")
                converted = {_snake(k): v for k, v in row.items()}
                if table == "games":
                    converted = {**SNAPSHOT_DEFAULTS, **converted}
                written.append(converted)
    return out
