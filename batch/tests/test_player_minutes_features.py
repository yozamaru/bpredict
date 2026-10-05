"""選手視点の特徴量と第2段（詳細設計 2.3 / 2.3.1）。

リーク検証は `test_leakage.py` にある。ここでは定義どおりの値になるかを見る。

| テスト | どの規約か |
|---|---|
| `test_four_columns` | 第2段の列は4本（2.3.1 の表） |
| `test_minutes_is_the_mean_of_the_window` | 直近N試合の平均 |
| `test_window_does_not_cross_season_boundary` | シーズン境界を越えない |
| `test_row_is_dropped_without_history` | 過去が無ければ行を落とす |
| `test_other_clubs_appearances_are_not_counted` | 所属は実績で判定する（規約6） |
| `test_stage_three_is_not_implemented` | 組めない段を黙って作らない |
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from batch.features.base import build_context
from batch.features.constants import RECENT_WINDOWS
from batch.features.dataset import export_sqlite
from batch.features.errors import FeatureError
from batch.features.player_rate import (
    DEFAULTS,
    LONG_WINDOW,
    MINUTES_KEYS,
    SHORT_WINDOW,
    build_minutes_features,
    minutes_recent,
    starter_rate,
)


def _appearance(con: sqlite3.Connection, offset: int = 0) -> tuple[str, datetime, str, str]:
    """終盤の試合から、出場実績のある選手を1人選ぶ。"""
    row = con.execute(
        "SELECT g.id, g.tipoff_at, p.club_id, p.player_id"
        "  FROM games g JOIN player_game_stats p ON p.game_id = g.id"
        " WHERE g.status = 'FINISHED' AND p.minutes IS NOT NULL"
        " ORDER BY g.game_date DESC, g.tipoff_at DESC, p.player_id"
        " LIMIT 1 OFFSET ?",
        (offset,),
    ).fetchone()
    return str(row[0]), datetime.fromisoformat(str(row[1])), str(row[2]), str(row[3])


# --- 列の形 ---

def test_four_columns() -> None:
    """**第2段の列は4本**（2.3.1 の表）。"""
    assert MINUTES_KEYS == (
        "minutes_l5_player", "minutes_l10_player", "is_starter_l5", "team_minutes_lost")


def test_windows_are_existing_constants() -> None:
    """**新しい定数を増やさない**（`RECENT_WINDOWS` の 5 と 10）。"""
    assert (SHORT_WINDOW, LONG_WINDOW) == RECENT_WINDOWS == (5, 10)


def test_defaults_cover_only_two_columns() -> None:
    """`minutes_l5_player` は既定値を持たない（欠けたら行を落とす。2.3.1）。"""
    assert DEFAULTS == {"is_starter_l5": 0.0, "team_minutes_lost": 0.0}
    assert "minutes_l5_player" not in DEFAULTS
    assert "minutes_l10_player" not in DEFAULTS


def test_row_keys_are_fixed(seeded_db: sqlite3.Connection) -> None:
    game_id, as_of, club_id, player_id = _appearance(seeded_db)
    row = build_minutes_features(
        game_id, as_of, export_sqlite(seeded_db), club_id, player_id)
    if row is None:
        pytest.skip("この選手は過去が無く行が落ちた")
    assert tuple(row) == MINUTES_KEYS


def test_club_outside_the_game_is_rejected(seeded_db: sqlite3.Connection) -> None:
    """**この試合に出場しないクラブを渡したら落とす。** 黙って計算しない。"""
    game_id, as_of, _club_id, player_id = _appearance(seeded_db)
    other = seeded_db.execute(
        "SELECT home_club_id, away_club_id FROM games WHERE id = ?", (game_id,)).fetchone()
    outsider = seeded_db.execute(
        "SELECT id FROM clubs WHERE id NOT IN (?, ?) LIMIT 1",
        (str(other[0]), str(other[1]))).fetchone()
    with pytest.raises(FeatureError, match="出場しないクラブ"):
        build_minutes_features(
            game_id, as_of, export_sqlite(seeded_db), str(outsider[0]), player_id)


# --- 集計の仕方 ---

def test_minutes_is_the_mean_of_the_window(seeded_db: sqlite3.Connection) -> None:
    """直近N試合の**平均**であること。"""
    game_id, as_of, club_id, player_id = _appearance(seeded_db)
    context = build_context(game_id, as_of, export_sqlite(seeded_db))
    history = context.player_history(club_id, season_only=True)
    mine = history[history["player_id"].astype(str) == player_id]
    mine = mine.sort_values("game_date", ascending=False, kind="stable")
    if mine.empty:
        pytest.skip("この選手は過去が無い")

    for window in (SHORT_WINDOW, LONG_WINDOW):
        expected = float(mine.head(window)["minutes"].dropna().mean())
        assert minutes_recent(context, club_id, player_id, window) == pytest.approx(expected)


def test_the_two_windows_read_different_games(seeded_db: sqlite3.Connection) -> None:
    """**5試合窓と10試合窓が別の試合を見ていること。**

    **合成シードは選手ごとに出場時間が一定である**（64選手で7種類。各選手が毎試合
    同じ分数）。そのままでは2つの列が常に一致し、**窓が効いていなくても通る**。
    したがって検査の条件をこちらで作る — 6〜10試合前だけ分数を変え、
    10試合窓にだけ入ることを確かめる。
    """
    game_id, as_of, club_id, player_id = _appearance(seeded_db)
    past = seeded_db.execute(
        "SELECT p.game_id FROM player_game_stats p JOIN games g ON g.id = p.game_id"
        " WHERE p.player_id = ? AND g.game_date < (SELECT game_date FROM games WHERE id = ?)"
        " ORDER BY g.game_date DESC LIMIT 10",
        (player_id, game_id)).fetchall()
    assert len(past) == LONG_WINDOW, (
        f"10試合ぶんの過去が要る（いまは {len(past)}）")

    older = [str(r[0]) for r in past[SHORT_WINDOW:]]
    seeded_db.executemany(
        "UPDATE player_game_stats SET minutes = 1.0 WHERE game_id = ? AND player_id = ?",
        [(g, player_id) for g in older])
    seeded_db.commit()

    built = build_minutes_features(
        game_id, as_of, export_sqlite(seeded_db), club_id, player_id)
    assert built is not None
    assert built["minutes_l5_player"] != pytest.approx(built["minutes_l10_player"]), (
        "6〜10試合前だけ変えても2つの列が一致する（窓が効いていない）")
    assert built["minutes_l10_player"] < built["minutes_l5_player"], (
        "古い5試合を 1.0 にしたので10試合窓の方が小さくなる"
    )


def test_starter_rate_is_the_mean_of_started(seeded_db: sqlite3.Connection) -> None:
    game_id, as_of, club_id, player_id = _appearance(seeded_db)
    context = build_context(game_id, as_of, export_sqlite(seeded_db))
    history = context.player_history(club_id, season_only=True)
    mine = history[history["player_id"].astype(str) == player_id]
    mine = mine.sort_values("game_date", ascending=False, kind="stable")
    if mine.empty:
        pytest.skip("この選手は過去が無い")
    expected = mine.head(SHORT_WINDOW)["started"].dropna()
    if expected.empty:
        pytest.skip("`started` が全件欠損")
    assert starter_rate(context, club_id, player_id) == pytest.approx(
        float(expected.mean()))


def test_window_does_not_cross_season_boundary(seeded_db: sqlite3.Connection) -> None:
    """**シーズン境界を越えない**（2.3.1。`winrate_recent` と同じ規約）。

    2季目の序盤を狙う。**試合を先に選ぶ** — JOIN した行に OFFSET をかけると
    1試合に16行あるため同一試合の中を指し、当季の過去が0件になって検査が
    空振りする（最初に書いた版がそうだった）。
    """
    second = seeded_db.execute(
        "SELECT season_id FROM games GROUP BY season_id"
        " ORDER BY MIN(game_date) DESC LIMIT 1").fetchone()[0]
    game = seeded_db.execute(
        "SELECT id, tipoff_at FROM games WHERE season_id = ? AND status = 'FINISHED'"
        " ORDER BY game_date ASC, tipoff_at ASC LIMIT 1 OFFSET 8",
        (second,)).fetchone()
    game_id, as_of = str(game[0]), datetime.fromisoformat(str(game[1]))
    row = seeded_db.execute(
        "SELECT club_id, player_id FROM player_game_stats WHERE game_id = ?"
        " ORDER BY player_id LIMIT 1", (game_id,)).fetchone()
    club_id, player_id = str(row[0]), str(row[1])
    context = build_context(game_id, as_of, export_sqlite(seeded_db))

    def mine(season_only: bool):
        history = context.player_history(club_id, season_only=season_only)
        picked = history[history["player_id"].astype(str) == player_id]
        return picked.sort_values("game_date", ascending=False, kind="stable")

    inside, crossing = mine(True), mine(False)
    assert not inside.empty, "当季の過去が0件では検査にならない"
    assert len(inside) < len(crossing), (
        f"境界をまたぐ条件を作れていない（当季 {len(inside)} / 全期間 {len(crossing)}）")

    # **合成シードは選手ごとに分数が一定**なので、前季の行だけ変えて差を作る
    previous = set(crossing["game_id"].astype(str)) - set(inside["game_id"].astype(str))
    seeded_db.executemany(
        "UPDATE player_game_stats SET minutes = 2.0 WHERE game_id = ? AND player_id = ?",
        [(g, player_id) for g in sorted(previous)])
    seeded_db.commit()

    context = build_context(game_id, as_of, export_sqlite(seeded_db))
    inside, crossing = mine(True), mine(False)
    expected = float(inside.head(LONG_WINDOW)["minutes"].dropna().mean())
    other = float(crossing.head(LONG_WINDOW)["minutes"].dropna().mean())
    assert expected != pytest.approx(other), "越えても値が同じなら検査にならない"
    assert minutes_recent(context, club_id, player_id, LONG_WINDOW) == pytest.approx(
        expected)


def test_other_clubs_appearances_are_not_counted(seeded_db: sqlite3.Connection) -> None:
    """**所属は `player_game_stats.club_id`（実績）で判定する**（2.1 の規約6）。

    同じ選手が別クラブで出た行は、こちらのクラブの窓に入らない。
    """
    game_id, as_of, club_id, player_id = _appearance(seeded_db)
    context = build_context(game_id, as_of, export_sqlite(seeded_db))
    history = context.player_history(club_id, season_only=True)
    mine = history[history["player_id"].astype(str) == player_id]
    assert not mine.empty or minutes_recent(
        context, club_id, player_id, SHORT_WINDOW) is None
    assert set(mine["club_id"].astype(str)) <= {club_id}


# --- 行を落とす ---

def test_row_is_dropped_without_history(seeded_db: sqlite3.Connection) -> None:
    """**過去が1件も無ければ `None` を返す**（行を落とす。2.3.1）。

    シーズン最初の試合では、その選手の当季の過去が空である。
    """
    row = seeded_db.execute(
        "SELECT g.id, g.tipoff_at, p.club_id, p.player_id"
        "  FROM games g JOIN player_game_stats p ON p.game_id = g.id"
        " WHERE g.status = 'FINISHED'"
        " ORDER BY g.game_date ASC, g.tipoff_at ASC, p.player_id LIMIT 1").fetchone()
    built = build_minutes_features(
        str(row[0]), datetime.fromisoformat(str(row[1])),
        export_sqlite(seeded_db), str(row[2]), str(row[3]))
    assert built is None


def test_missing_minutes_drops_the_row(seeded_db: sqlite3.Connection) -> None:
    """**`minutes` が全件 NULL の選手は行を落とす。** 0 で埋めない。"""
    game_id, as_of, club_id, player_id = _appearance(seeded_db)
    seeded_db.execute(
        "UPDATE player_game_stats SET minutes = NULL WHERE player_id = ?", (player_id,))
    seeded_db.commit()
    built = build_minutes_features(
        game_id, as_of, export_sqlite(seeded_db), club_id, player_id)
    assert built is None


# --- 組めない段を作らない ---

def test_stage_three_is_not_implemented() -> None:
    """**第3段を黙って作らない**（2.3.1）。

    `k` / `prior` / `usage_l10` が未定義である。**実装が先に出ると、設計に無い
    仕様が「実装しながら決めた値」として残る。**

    第1段は v1.106 で候補集合を定めてから実装した（`test_player_avail_*`）。
    """
    import batch.model.train_player as tp
    assert not hasattr(tp, "evaluate_rates"), "第3段は設計が決まってから実装する"

    from batch.features import player_rate
    for name in ("usage_l10", "shrunk_pct"):
        assert not hasattr(player_rate, name), f"{name} は 2.3.1 で未定義である"
