"""第3段（PlayerRates）の特徴量（詳細設計 2.3.1）。

リーク検証は `test_leakage.py` にある。ここでは定義どおりの値になるかを見る。

| テスト | どの規約か |
|---|---|
| `test_matrix_has_fifty_seven_columns` | 14本を1枚の行列で持つ（2.3.1 の表） |
| `test_percentage_models_take_the_shrunk_columns` | 行列6列 → モデル4列 |
| `test_count_models_take_nine_columns` | カウントは9列 |
| `test_usage_is_the_standard_formula` | **業界標準の USG%**（運営者の判断） |
| `test_usage_averages_about_one_fifth` | 分母は `Tm MP / 5`。200分で割らない |
| `test_per_minute_is_sum_over_sum` | **合計 ÷ 合計**（率の平均ではない） |
| `test_pct_sums_are_raw` | 行列はシュリンク前の生の合計 |
| `test_window_does_not_cross_season_boundary` | 直近10試合の窓は季を越えない |
| `test_prior_is_this_season` / `test_prior_is_nan_in_the_first_season` | 当季。無ければ前季 |
| `test_position_is_nan_when_unregistered` | 未登録を 0 にしない |
| `test_pred_minutes_is_nan_in_the_matrix` | **第2段の出力は fold ごとに埋める** |
| `test_row_is_dropped_without_history` | 第2段が出せない選手は第3段も出せない |
| `test_opponent_columns_describe_the_opponent` | 相手の列に自分の値を入れない |
| `test_unknown_target_is_rejected` | 14項目にない名前を黙って受けない |
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime

import pytest

from batch.features import team_rate
from batch.features.base import Context, build_context
from batch.features.dataset import Dataset, export_sqlite
from batch.features.errors import FeatureError
from batch.features.player_rate import (
    FT_PLAY_COEFFICIENT,
    LONG_WINDOW,
    PLAYERS_ON_COURT,
    TEAM_MINUTES,
    all_rate_matrix_keys,
    build_rate_features,
    league_prior,
    pct_sums,
    per_min_level,
    position_code,
    rate_matrix_keys,
    rate_model_keys,
    rate_row,
    shrink,
    usage,
)

SECOND_SEASON = "2025-26-B1"
FIRST_SEASON = "2024-25-B1"


def _target(
    con: sqlite3.Connection, season: str = SECOND_SEASON, offset: int = 0,
) -> tuple[str, datetime, str, str]:
    """その季の終盤の試合から、出場実績のある選手を1人選ぶ。"""
    row = con.execute(
        "SELECT g.id, g.tipoff_at, p.club_id, p.player_id"
        "  FROM games g JOIN player_game_stats p ON p.game_id = g.id"
        " WHERE g.status = 'FINISHED' AND g.season_id = ? AND p.minutes IS NOT NULL"
        " ORDER BY g.game_date DESC, g.tipoff_at DESC, p.player_id"
        " LIMIT 1 OFFSET ?",
        (season, offset),
    ).fetchone()
    return str(row[0]), datetime.fromisoformat(str(row[1])), str(row[2]), str(row[3])


def _context(
    con: sqlite3.Connection, game_id: str, as_of: datetime,
) -> Context:
    return build_context(game_id, as_of, export_sqlite(con))


# --- 列の構成 ---

def test_matrix_has_fifty_seven_columns() -> None:
    """共有6 + カウント11×3 + 成功率3×6 = 57（2.3.1 の表）。"""
    keys = all_rate_matrix_keys()
    assert len(keys) == len(set(keys))
    assert len(keys) == 6 + 11 * 3 + 3 * 6


def test_percentage_models_take_the_shrunk_columns() -> None:
    """**行列は生の合計、モデルはシュリンク済み**（`k` は学習時に当てる）。"""
    matrix = rate_matrix_keys("fg3_pct")
    model = rate_model_keys("fg3_pct")
    assert len(matrix) == 6 + 6
    assert len(model) == 6 + 4
    assert "fg3_pct_made_l10" in matrix and "fg3_pct_made_l10" not in model
    assert "fg3_pct_shrunk_l10" in model and "fg3_pct_shrunk_l10" not in matrix
    # 試投数は**学習重みとしても**モデルに渡る（要件 6.8.4）
    assert "fg3_pct_att_l10" in model


def test_count_models_take_nine_columns() -> None:
    assert len(rate_model_keys("fg3a")) == 9
    assert rate_model_keys("fg3a") == rate_matrix_keys("fg3a")


def test_every_target_has_keys() -> None:
    assert len(team_rate.TARGETS) == 14
    for target in team_rate.TARGETS:
        assert len(rate_model_keys(target)) in (9, 10)


def test_unknown_target_is_rejected() -> None:
    """**14項目にない名前を黙って受けない。**"""
    for call in (rate_matrix_keys, rate_model_keys):
        with pytest.raises(FeatureError, match="14項目"):
            call("pts")


# --- 使用率 ---

def test_usage_is_the_standard_formula(seeded_db: sqlite3.Connection) -> None:
    """手計算と一致すること。

        攻撃終了数 = FGA + 0.44 × FTA + TOV
        usage = (本人 ÷ 本人の分) ÷ (チーム ÷ (200 ÷ 5))
    """
    game_id, as_of, club_id, player_id = _target(seeded_db)
    context = _context(seeded_db, game_id, as_of)
    found = usage(context, club_id, player_id)
    assert found is not None

    boundary = as_of.isoformat(timespec="seconds").replace("+00:00", "Z")
    own = seeded_db.execute(
        "SELECT SUM(p.fg2a + p.fg3a + ? * p.fta + p.tov), SUM(p.minutes)"
        "  FROM player_game_stats p JOIN games g ON g.id = p.game_id"
        " WHERE p.player_id = ? AND p.club_id = ? AND g.season_id = ?"
        "   AND g.finished_at <= ? AND g.id <> ?"
        "   AND g.id IN (SELECT g2.id FROM games g2"
        "                 JOIN player_game_stats p2 ON p2.game_id = g2.id"
        "                WHERE p2.player_id = p.player_id AND p2.club_id = p.club_id"
        "                  AND g2.season_id = ? AND g2.finished_at <= ? AND g2.id <> ?"
        "                ORDER BY g2.finished_at DESC, g2.id DESC LIMIT ?)",
        (FT_PLAY_COEFFICIENT, player_id, club_id, SECOND_SEASON, boundary, game_id,
         SECOND_SEASON, boundary, game_id, LONG_WINDOW),
    ).fetchone()
    team = seeded_db.execute(
        "SELECT SUM(t.fg2a + t.fg3a + ? * t.fta + t.tov), COUNT(*)"
        "  FROM team_game_stats t JOIN games g ON g.id = t.game_id"
        " WHERE t.club_id = ? AND g.season_id = ? AND g.finished_at <= ? AND g.id <> ?"
        "   AND g.id IN (SELECT g2.id FROM games g2 JOIN team_game_stats t2"
        "                  ON t2.game_id = g2.id AND t2.club_id = t.club_id"
        "                WHERE g2.season_id = ? AND g2.finished_at <= ? AND g2.id <> ?"
        "                ORDER BY g2.finished_at DESC, g2.id DESC LIMIT ?)",
        (FT_PLAY_COEFFICIENT, club_id, SECOND_SEASON, boundary, game_id,
         SECOND_SEASON, boundary, game_id, LONG_WINDOW),
    ).fetchone()

    expected = (float(own[0]) / float(own[1])) / (
        float(team[0]) / (TEAM_MINUTES / PLAYERS_ON_COURT * int(team[1])))
    assert found == pytest.approx(expected, rel=1e-9)


def test_usage_averages_about_one_fifth(seeded_db: sqlite3.Connection) -> None:
    """**分母を 200分にしない。** 200で割ると平均が 1.0 になる別の量である。

    USG% の平均は 20% 付近に落ち着く（1人が1/5の時間、攻撃終了の 1/5 を担う）。
    """
    game_id, as_of, club_id, _ = _target(seeded_db)
    context = _context(seeded_db, game_id, as_of)
    players = [
        str(r[0]) for r in seeded_db.execute(
            "SELECT DISTINCT player_id FROM player_game_stats WHERE club_id = ?",
            (club_id,))
    ]
    found = [usage(context, club_id, p) for p in players]
    known = [v for v in found if v is not None]
    assert len(known) >= 5
    assert 0.05 < sum(known) / len(known) < 0.45


# --- 水準 ---

def test_per_minute_is_sum_over_sum(seeded_db: sqlite3.Connection) -> None:
    """**合計 ÷ 合計。** 試合ごとのレートを平均しない（`off_rating` の規則）。"""
    game_id, as_of, club_id, player_id = _target(seeded_db)
    context = _context(seeded_db, game_id, as_of)
    found = per_min_level(context, club_id, player_id, "ast", season_only=True)
    assert found is not None

    boundary = as_of.isoformat(timespec="seconds").replace("+00:00", "Z")
    totals = seeded_db.execute(
        "SELECT SUM(p.ast), SUM(p.minutes) FROM player_game_stats p"
        "  JOIN games g ON g.id = p.game_id"
        " WHERE p.player_id = ? AND p.club_id = ? AND g.season_id = ?"
        "   AND g.finished_at <= ? AND g.id <> ?",
        (player_id, club_id, SECOND_SEASON, boundary, game_id),
    ).fetchone()
    assert found == pytest.approx(float(totals[0]) / float(totals[1]), rel=1e-9)


def test_pct_sums_are_raw(seeded_db: sqlite3.Connection) -> None:
    """行列が持つのは **(Σ成功数, Σ試投数)**。シュリンクは学習時に当てる。"""
    game_id, as_of, club_id, player_id = _target(seeded_db)
    context = _context(seeded_db, game_id, as_of)
    found = pct_sums(context, club_id, player_id, "fg2_pct", season_only=True)
    assert found is not None

    boundary = as_of.isoformat(timespec="seconds").replace("+00:00", "Z")
    totals = seeded_db.execute(
        "SELECT SUM(p.fg2m), SUM(p.fg2a) FROM player_game_stats p"
        "  JOIN games g ON g.id = p.game_id"
        " WHERE p.player_id = ? AND p.club_id = ? AND g.season_id = ?"
        "   AND g.finished_at <= ? AND g.id <> ?",
        (player_id, club_id, SECOND_SEASON, boundary, game_id),
    ).fetchone()
    assert found[0] == pytest.approx(float(totals[0]))
    assert found[1] == pytest.approx(float(totals[1]))


def test_window_does_not_cross_season_boundary(seeded_db: sqlite3.Connection) -> None:
    """**直近10試合は季を越えない**（`winrate_recent` の規約に合わせる）。

    第2季の序盤では、第1季の試合が窓に入ってはならない。
    """
    # **当季に2〜9試合、かつ前季にも実績がある選手を選ぶ。** 窓（10）に届かない
    # ため、季を越えていなければ「直近10試合」と「当季通算」が一致する。
    # 越えていれば前季の試投が足されて直近10試合の方が大きくなる
    row = seeded_db.execute(
        "SELECT g.id, g.tipoff_at, p.club_id, p.player_id FROM games g"
        "  JOIN player_game_stats p ON p.game_id = g.id"
        " WHERE g.status = 'FINISHED' AND g.season_id = :late"
        "   AND (SELECT COUNT(*) FROM player_game_stats q JOIN games h ON h.id = q.game_id"
        "         WHERE q.player_id = p.player_id AND q.club_id = p.club_id"
        "           AND h.season_id = :late AND h.finished_at < g.finished_at)"
        "       BETWEEN 2 AND 9"
        "   AND EXISTS (SELECT 1 FROM player_game_stats q JOIN games h ON h.id = q.game_id"
        "                WHERE q.player_id = p.player_id AND h.season_id = :early)"
        " ORDER BY g.game_date, g.id, p.player_id LIMIT 1",
        {"late": SECOND_SEASON, "early": FIRST_SEASON}).fetchone()
    assert row is not None, "条件に合う選手がいない。テストが空振りしている"
    game_id, as_of = str(row[0]), datetime.fromisoformat(str(row[1]))
    club_id, player_id = str(row[2]), str(row[3])
    context = _context(seeded_db, game_id, as_of)

    recent = pct_sums(context, club_id, player_id, "ft_pct", season_only=False)
    season = pct_sums(context, club_id, player_id, "ft_pct", season_only=True)
    assert recent is not None and season is not None
    # 当季の試合数が窓（10）に届かないため、両者は一致する。
    # 季を越えていれば直近10試合の方が大きくなる
    assert recent == season


# --- リーグ平均 ---

def test_prior_is_this_season(seeded_db: sqlite3.Connection) -> None:
    """**当季のここまでの集計**（`as_of` に依る。定数ではない）。"""
    game_id, as_of, _, _ = _target(seeded_db)
    context = _context(seeded_db, game_id, as_of)
    found = league_prior(context, "fg2_pct")
    assert found is not None

    boundary = as_of.isoformat(timespec="seconds").replace("+00:00", "Z")
    totals = seeded_db.execute(
        "SELECT SUM(t.fg2m), SUM(t.fg2a) FROM team_game_stats t"
        "  JOIN games g ON g.id = t.game_id"
        " WHERE g.season_id = ? AND g.finished_at <= ?",
        (SECOND_SEASON, boundary)).fetchone()
    assert found == pytest.approx(float(totals[0]) / float(totals[1]), rel=1e-9)


def test_prior_is_nan_in_the_first_season(seeded_db: sqlite3.Connection) -> None:
    """**データ上の最初の季の開幕戦では前季が無い。** NaN になる（2.3.1）。"""
    row = seeded_db.execute(
        "SELECT id, tipoff_at FROM games WHERE status = 'FINISHED' AND season_id = ?"
        " ORDER BY game_date, id LIMIT 1", (FIRST_SEASON,)).fetchone()
    context = _context(
        seeded_db, str(row[0]), datetime.fromisoformat(str(row[1])))
    assert league_prior(context, "fg3_pct") is None


def test_prior_falls_back_to_the_previous_season(seeded_db: sqlite3.Connection) -> None:
    """**当季が0本のときだけ前季を使う**（運営者の判断。2.3.1）。"""
    row = seeded_db.execute(
        "SELECT id, tipoff_at FROM games WHERE status = 'FINISHED' AND season_id = ?"
        " ORDER BY game_date, id LIMIT 1", (SECOND_SEASON,)).fetchone()
    context = _context(
        seeded_db, str(row[0]), datetime.fromisoformat(str(row[1])))
    found = league_prior(context, "fg3_pct")
    assert found is not None, "前季があるのに NaN になった"

    totals = seeded_db.execute(
        "SELECT SUM(t.fg3m), SUM(t.fg3a) FROM team_game_stats t"
        "  JOIN games g ON g.id = t.game_id WHERE g.season_id = ?",
        (FIRST_SEASON,)).fetchone()
    assert found == pytest.approx(float(totals[0]) / float(totals[1]), rel=1e-9)


# --- ポジション ---

def test_position_is_nan_when_unregistered(seeded_db: sqlite3.Connection) -> None:
    """**未登録を 0 にしない**（0 は PG である。2.3.1）。"""
    game_id, as_of, _, player_id = _target(seeded_db)
    context = _context(seeded_db, game_id, as_of)
    seeded_db.execute("DELETE FROM player_seasons WHERE player_id = ?", (player_id,))
    seeded_db.commit()
    assert position_code(
        _context(seeded_db, game_id, as_of), player_id) is None
    assert context is not None


# --- 行 ---

def test_pred_minutes_is_nan_in_the_matrix(seeded_db: sqlite3.Connection) -> None:
    """**第2段の出力は fold ごとに埋める**（2.3.1）。列は落とさない。"""
    game_id, as_of, club_id, player_id = _target(seeded_db)
    row = build_rate_features(
        game_id, as_of, export_sqlite(seeded_db), club_id, player_id)
    assert row is not None
    assert "pred_minutes" in row
    assert math.isnan(row["pred_minutes"])


def test_row_is_dropped_without_history(seeded_db: sqlite3.Connection) -> None:
    """**第2段が出せない選手は第3段も出せない**（レートに掛ける相手が無い）。"""
    row = seeded_db.execute(
        "SELECT g.id, g.tipoff_at, p.club_id, p.player_id"
        "  FROM games g JOIN player_game_stats p ON p.game_id = g.id"
        " WHERE g.status = 'FINISHED' AND g.season_id = ?"
        " ORDER BY g.game_date, g.id, p.player_id LIMIT 1", (FIRST_SEASON,)).fetchone()
    assert build_rate_features(
        str(row[0]), datetime.fromisoformat(str(row[1])),
        export_sqlite(seeded_db), str(row[2]), str(row[3])) is None


def test_other_clubs_are_rejected(seeded_db: sqlite3.Connection) -> None:
    """その試合に出場しないクラブを渡すのは呼び出し側の誤りである。"""
    game_id, as_of, _, player_id = _target(seeded_db)
    context = _context(seeded_db, game_id, as_of)
    with pytest.raises(FeatureError, match="出場しない"):
        rate_row(context, "存在しないクラブ", player_id)


def test_opponent_columns_describe_the_opponent(seeded_db: sqlite3.Connection) -> None:
    """**相手の列に自分の値を入れない。**

    ホーム側の行とアウェイ側の行で、`opponent_*` が入れ替わること。
    """
    game_id, as_of, club_id, player_id = _target(seeded_db)
    dataset: Dataset = export_sqlite(seeded_db)
    context = build_context(game_id, as_of, dataset)
    other = (
        context.away_club_id if club_id == context.home_club_id
        else context.home_club_id)
    opponent_player = str(seeded_db.execute(
        "SELECT player_id FROM player_game_stats WHERE game_id = ? AND club_id = ?"
        " ORDER BY player_id LIMIT 1", (game_id, other)).fetchone()[0])

    mine = rate_row(context, club_id, player_id)
    theirs = rate_row(context, other, opponent_player)
    assert mine is not None and theirs is not None
    assert mine["opponent_pace"] != pytest.approx(theirs["opponent_pace"])


def test_shrink_is_clipped() -> None:
    """**出力は `[0.01, 0.99]`**（ロジット変換の定義域。2.4）。"""
    assert shrink(0.0, 0.0, 0.0, 20.0) == 0.01
    assert shrink(100.0, 100.0, 1.0, 20.0) == 0.99
    assert shrink(10.0, 30.0, 0.33, 20.0) == pytest.approx((10 + 20 * 0.33) / 50)


def test_shrink_rejects_a_non_positive_k() -> None:
    with pytest.raises(FeatureError, match="k"):
        shrink(1.0, 2.0, 0.5, 0.0)
