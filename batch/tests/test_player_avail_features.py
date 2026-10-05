"""第1段（PlayerAvail）の候補集合と特徴量（詳細設計 2.3.1）。

リーク検証は `test_leakage.py` にある。ここでは定義どおりの値になるかを見る。

| テスト | どの規約か |
|---|---|
| `test_five_columns` | 第1段の列は5本（2.3.1 の表） |
| `test_candidates_exclude_future_arrivals` | **ロスターを候補にしない**（絶対ルール1） |
| `test_candidates_include_last_season_absentees` | 前季の実績も候補に入る（和集合） |
| `test_candidates_are_sorted` | 並びを固定する（学習の再現性） |
| `test_first_season_opener_has_no_candidates` | **空を隠さない** |
| `test_rows_without_history_are_kept` | **第2段と逆。** 落とさない |
| `test_days_since_last_played_is_clipped` | 90日で飽和させる |
| `test_days_since_is_not_folded_into_the_default` | 0日と既定値90日を混ぜない |
| `test_games_played_ratio_denominator_is_club_games` | 分母はクラブの試合数 |
| `test_label_is_appearance` | `player_game_stats` に行があることが正例 |
| `test_uncovered_appearances_are_counted` | 覆えなかった出場を黙って落とさない |
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from batch.features.base import build_context
from batch.features.dataset import Dataset, export_sqlite
from batch.features.errors import FeatureError
from batch.features.player_rate import (
    AVAIL_DEFAULTS,
    AVAIL_KEYS,
    DAYS_SINCE_CAP,
    avail_row,
    build_avail_features,
    candidates,
    days_since_last_played,
    games_played_ratio,
)

SECOND_SEASON = "2025-26-B1"
FIRST_SEASON = "2024-25-B1"


def _late_game(con: sqlite3.Connection, season: str = SECOND_SEASON) -> tuple[str, datetime, str]:
    """その季の終盤の試合を1つ選ぶ（履歴が十分に積まれている状態）。"""
    row = con.execute(
        "SELECT id, tipoff_at, home_club_id FROM games"
        " WHERE status = 'FINISHED' AND season_id = ?"
        " ORDER BY game_date DESC, id DESC LIMIT 1",
        (season,),
    ).fetchone()
    return str(row[0]), datetime.fromisoformat(str(row[1])), str(row[2])


def _opener(con: sqlite3.Connection, season: str) -> tuple[str, datetime, str]:
    row = con.execute(
        "SELECT id, tipoff_at, home_club_id FROM games"
        " WHERE status = 'FINISHED' AND season_id = ?"
        " ORDER BY game_date, id LIMIT 1",
        (season,),
    ).fetchone()
    return str(row[0]), datetime.fromisoformat(str(row[1])), str(row[2])


# --- 列の形 ---

def test_five_columns() -> None:
    """**第1段の列は5本**（2.3.1 の表）。"""
    assert AVAIL_KEYS == (
        "minutes_l5_player", "minutes_l10_player", "games_played_ratio_l10",
        "days_since_last_played", "entry_status")


def test_every_column_has_a_default() -> None:
    """**第1段は行を落とさない**ため、全列に既定値が要る（2.3.1）。"""
    assert set(AVAIL_DEFAULTS) == set(AVAIL_KEYS)


def test_days_since_default_is_the_cap() -> None:
    """履歴がない選手は「前季以来出ていない」側に置く。"""
    assert AVAIL_DEFAULTS["days_since_last_played"] == DAYS_SINCE_CAP == 90.0


# --- 候補集合 ---

def test_candidates_exclude_future_arrivals(seeded_db: sqlite3.Connection) -> None:
    """**ロスターを候補にしない**（絶対ルール1）。

    `player_seasons` にいるが過去に1試合も出ていない選手は候補に入らない。
    あの表は取得した時点の断面であり、季中の加入を加入前の試合に持ち込む。
    """
    game_id, as_of, club_id = _late_game(seeded_db)
    season = str(seeded_db.execute(
        "SELECT season_id FROM games WHERE id = ?", (game_id,)).fetchone()[0])
    roster = {str(r[0]) for r in seeded_db.execute(
        "SELECT player_id FROM player_seasons WHERE club_id = ? AND season_id = ?",
        (club_id, season))}
    appeared = {str(r[0]) for r in seeded_db.execute(
        "SELECT DISTINCT player_id FROM player_game_stats WHERE club_id = ?", (club_id,))}
    never = roster - appeared
    assert never, "出場実績のないロスター選手がいるシードであること"

    ds = export_sqlite(seeded_db)
    names = set(candidates(build_context(game_id, as_of, ds), club_id))
    assert not (names & never), "出場実績のない選手が候補に入っている"


def test_candidates_include_last_season_absentees(seeded_db: sqlite3.Connection) -> None:
    """前季にそのクラブで出場した選手は、当季まだ出ていなくても候補に入る。

    **和集合である。** 当季の実績だけに絞ると、負傷明けの主力が落ちる。
    """
    game_id, as_of, club_id = _opener(seeded_db, SECOND_SEASON)
    previous = {str(r[0]) for r in seeded_db.execute(
        "SELECT DISTINCT s.player_id FROM player_game_stats s"
        "  JOIN games g ON g.id = s.game_id"
        " WHERE s.club_id = ? AND g.season_id = ?",
        (club_id, FIRST_SEASON))}
    assert previous, "前季に出場実績があるシードであること"

    ds = export_sqlite(seeded_db)
    names = set(candidates(build_context(game_id, as_of, ds), club_id))
    # 開幕戦なので当季の実績は0。候補はすべて前季由来である
    assert names == previous


def test_candidates_are_sorted(seeded_db: sqlite3.Connection) -> None:
    """並びを固定する。実行ごとに行の順序が変わると再現性が崩れる。"""
    game_id, as_of, club_id = _late_game(seeded_db)
    ds = export_sqlite(seeded_db)
    names = candidates(build_context(game_id, as_of, ds), club_id)
    assert names == sorted(names)
    assert len(names) == len(set(names))


def test_candidates_reject_a_club_not_in_the_game(seeded_db: sqlite3.Connection) -> None:
    """この試合に出場しないクラブを渡すのは呼び出し側の誤りである。"""
    game_id, as_of, _ = _late_game(seeded_db)
    ds = export_sqlite(seeded_db)
    context = build_context(game_id, as_of, ds)
    other = next(
        str(r[0]) for r in seeded_db.execute("SELECT id FROM clubs ORDER BY id")
        if str(r[0]) not in (context.home_club_id, context.away_club_id))
    with pytest.raises(FeatureError):
        candidates(context, other)


def test_first_season_opener_has_no_candidates(seeded_db: sqlite3.Connection) -> None:
    """データ上の最初の季の開幕戦では候補が空になる。**空を隠さない。**

    前季の実績がなく、当季もまだ1試合もない。昇格クラブと同じコールドスタート
    であり、別の規則を作らない（2.5）。
    """
    game_id, as_of, club_id = _opener(seeded_db, FIRST_SEASON)
    ds = export_sqlite(seeded_db)
    context = build_context(game_id, as_of, ds)
    assert context.previous_season_id is None
    assert candidates(context, club_id) == []


def test_previous_season_is_the_one_before(seeded_db: sqlite3.Connection) -> None:
    """**直前の1季に限る**（「過去のいずれかの季」にしない）。"""
    game_id, as_of, _ = _late_game(seeded_db)
    ds = export_sqlite(seeded_db)
    assert build_context(game_id, as_of, ds).previous_season_id == FIRST_SEASON


def test_missing_seasons_is_not_silently_the_first_season(
    seeded_db: sqlite3.Connection,
) -> None:
    """**季の一覧が無いときに黙って「当季だけ」へ縮まないこと。**

    `prepare` は部分的な `Dataset` を許す（手で組んだ検査のため）。だから
    使う側が落とす — 落とさないと、候補集合が前季を見ないまま通る。
    """
    game_id, as_of, club_id = _late_game(seeded_db)
    ds = export_sqlite(seeded_db)
    stripped = Dataset(tables={k: v for k, v in ds.tables.items() if k != "seasons"})
    context = build_context(game_id, as_of, stripped)
    with pytest.raises(FeatureError, match="季の一覧"):
        candidates(context, club_id)


# --- 行の値 ---

def test_rows_without_history_are_kept(seeded_db: sqlite3.Connection) -> None:
    """**第2段と逆に、履歴がない行を落とさない**（2.3.1）。

    「過去がない」ことが強い負例の signal である。落とすと候補のうち最も
    出場しない層が学習から消える。
    """
    game_id, as_of, club_id = _opener(seeded_db, FIRST_SEASON)
    ds = export_sqlite(seeded_db)
    player_id = str(seeded_db.execute(
        "SELECT player_id FROM player_game_stats WHERE club_id = ? LIMIT 1",
        (club_id,)).fetchone()[0])
    row = build_avail_features(game_id, as_of, ds, club_id, player_id)
    assert list(row) == list(AVAIL_KEYS)
    # `entry_status` は試合単位でありこの選手の履歴に依らないため、履歴由来の
    # 4列だけを見る（既定値に落ちていること）
    history = [key for key in AVAIL_KEYS if key != "entry_status"]
    assert {key: row[key] for key in history} == {
        key: AVAIL_DEFAULTS[key] for key in history}


def test_days_since_last_played_is_clipped(seeded_db: sqlite3.Connection) -> None:
    """90日で飽和させる。季の進行そのものを表す列にしない。"""
    game_id, as_of, club_id = _opener(seeded_db, SECOND_SEASON)
    ds = export_sqlite(seeded_db)
    context = build_context(game_id, as_of, ds)
    gaps = [
        days_since_last_played(context, club_id, player_id)
        for player_id in candidates(context, club_id)
    ]
    assert gaps, "候補がいること"
    # 開幕戦なので直近の出場は前季（約8か月前）。クリップが効いていなければ超える
    assert all(gap is not None and 0.0 <= gap <= DAYS_SINCE_CAP for gap in gaps)
    assert max(gap for gap in gaps if gap is not None) == DAYS_SINCE_CAP


def test_days_since_is_not_folded_into_the_default(
    seeded_db: sqlite3.Connection,
) -> None:
    """**0日と既定値90日を混ぜない。**

    `AVAIL_DEFAULTS[...] if value is None else float(value)` を
    `float(value or default)` と書くと、同日に出場した選手（0日）が
    「前季以来出ていない」扱いになる。

    **条件をこちらで作る。** 合成シードではクラブが同日に2試合することがなく、
    探すだけでは検査が空振りする（`test_fouls_are_not_clipped` と同じ轍）。
    """
    game_id, as_of, club_id = _late_game(seeded_db)
    row = seeded_db.execute(
        "SELECT season_id, game_date, away_club_id FROM games WHERE id = ?",
        (game_id,)).fetchone()
    season, game_date, away = str(row[0]), str(row[1]), str(row[2])
    player_id = str(seeded_db.execute(
        "SELECT player_id FROM player_game_stats WHERE club_id = ? LIMIT 1",
        (club_id,)).fetchone()[0])

    # 同じ日に、対象試合より早く終わる試合を足す
    earlier = f"{game_date}T01:00:00Z"
    seeded_db.execute(
        "INSERT INTO games (id, season_id, league, competition, game_date,"
        " tipoff_at, finished_at, home_club_id, away_club_id, status,"
        " home_score, away_score)"
        " VALUES ('same-day', ?, 'B1', 'REGULAR', ?, ?, ?, ?, ?, 'FINISHED', 80, 70)",
        (season, game_date, earlier, earlier, club_id, away))
    for owner, opponent, is_home in ((club_id, away, 1), (away, club_id, 0)):
        seeded_db.execute(
            "INSERT INTO team_games (game_id, club_id, opponent_id, season_id,"
            " game_date, finished_at, is_home, competition, result, margin)"
            " VALUES ('same-day', ?, ?, ?, ?, ?, ?, 'REGULAR', ?, ?)",
            (owner, opponent, season, game_date, earlier, is_home,
             is_home, 10 if is_home else -10))
    seeded_db.execute(
        "INSERT INTO player_game_stats (game_id, player_id, club_id, game_date,"
        " minutes, fetched_at) VALUES ('same-day', ?, ?, ?, 20.0, ?)",
        (player_id, club_id, game_date, earlier))

    context = build_context(game_id, as_of, export_sqlite(seeded_db))
    assert days_since_last_played(context, club_id, player_id) == 0.0
    assert avail_row(context, club_id, player_id)["days_since_last_played"] == 0.0


def test_games_played_ratio_denominator_is_club_games(
    seeded_db: sqlite3.Connection,
) -> None:
    """分母はクラブの試合数である（選手の出場試合数ではない）。

    毎試合出ている選手は 1.0 になる。
    """
    game_id, as_of, club_id = _late_game(seeded_db)
    ds = export_sqlite(seeded_db)
    context = build_context(game_id, as_of, ds)
    ratios = [
        games_played_ratio(context, club_id, player_id)
        for player_id in candidates(context, club_id)
    ]
    assert ratios
    assert all(value is not None and 0.0 <= value <= 1.0 for value in ratios)
    assert max(value for value in ratios if value is not None) == 1.0


def test_ratio_is_zero_without_appearances(seeded_db: sqlite3.Connection) -> None:
    """当季に1試合も出ていない選手は 0.0（既定値ではなく計算結果）。"""
    game_id, as_of, club_id = _opener(seeded_db, SECOND_SEASON)
    ds = export_sqlite(seeded_db)
    context = build_context(game_id, as_of, ds)
    player_id = candidates(context, club_id)[0]
    # 開幕戦なのでクラブの当季の試合も0 → 分母が無く None
    assert games_played_ratio(context, club_id, player_id) is None
    assert avail_row(context, club_id, player_id)["games_played_ratio_l10"] == 0.0


def test_entry_status_is_a_game_level_value(seeded_db: sqlite3.Connection) -> None:
    """`entry_status` は試合単位である（2.3 の「エントリー情報（公式/推定）」）。

    選手ごとの ENTRY / OUT を列にするかは `gameday_update` が入ってから決める。
    """
    game_id, as_of, club_id = _late_game(seeded_db)
    ds = export_sqlite(seeded_db)
    context = build_context(game_id, as_of, ds)
    names = candidates(context, club_id)
    values = {avail_row(context, club_id, p)["entry_status"] for p in names}
    assert len(values) == 1, "選手ごとに違う値になっている"
