"""TeamRate の特徴量（詳細設計 2.2.1）。

リーク検証は `test_leakage.py` にある。ここでは定義どおりの値になるかを見る。

| テスト | 2.2.1 のどの規約か |
|---|---|
| `test_targets_match_reconcile` | 14項目が整合化の集合と一致する |
| `test_one_row_per_club` | 1行は「試合 × クラブ」。`is_home` が 1 と 0 の両方を取る |
| `test_six_columns_per_target` | 共有4列 ＋ 目的変数ごと2列 |
| `test_pct_is_sum_over_sum` | 成功率は Σ成功数 ÷ Σ試投数（率の平均ではない） |
| `test_own_level_does_not_cross_season_boundary` | 直近10試合はシーズンを越えない |
| `test_row_is_dropped_when_pace_is_missing` | `pace` が欠けたら行を落とす |
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime

import pytest

from batch.features.dataset import export_sqlite
from batch.features.errors import FeatureError
from batch.features.team_rate import (
    COUNT_TARGETS,
    PCT_TARGETS,
    TARGETS,
    WINDOW,
    all_feature_keys,
    build_team_rate_features,
    feature_keys,
    features_for,
    own_level,
    shared_keys,
)
from batch.model.reconcile import ATTEMPTS, COUNTS, PCTS


def _game(con: sqlite3.Connection, offset: int = 0) -> tuple[str, datetime, str, str]:
    row = con.execute(
        "SELECT id, tipoff_at, home_club_id, away_club_id FROM games"
        " WHERE status = 'FINISHED'"
        " ORDER BY game_date DESC, tipoff_at DESC LIMIT 1 OFFSET ?",
        (offset,),
    ).fetchone()
    return str(row[0]), datetime.fromisoformat(str(row[1])), str(row[2]), str(row[3])


# --- 目的変数の集合 ---

def test_targets_match_reconcile() -> None:
    """**整合化と同じ14項目であること。**

    層をまたいで定数を共有しない方針（`constants.py` の `FTA_COEFFICIENT`）のため
    2箇所に書いてあるが、**食い違ったら整合化が成立しない**。ここで固定する。
    """
    assert set(COUNT_TARGETS) == set(ATTEMPTS) | set(COUNTS)
    assert len(COUNT_TARGETS) == 11
    assert [(p, a) for p, _, a in PCT_TARGETS] == list(PCTS)
    assert len(TARGETS) == 14


def test_window_is_an_existing_constant() -> None:
    """**新しい定数を増やさない**（2.2.1）。`RECENT_WINDOWS` の 10 を使う。"""
    from batch.features.constants import RECENT_WINDOWS
    assert WINDOW == 10
    assert WINDOW in RECENT_WINDOWS


# --- 列の形 ---

def test_six_columns_per_target() -> None:
    """共有4列 ＋ 目的変数ごと2列 = 6列（案C）。"""
    assert shared_keys() == ("pace_own", "pace_opp", "is_home", "rest_days_own")
    for target in TARGETS:
        keys = feature_keys(target)
        assert len(keys) == 6
        assert keys[:4] == shared_keys()
        assert keys[4:] == (
            f"own_{target}_l10", f"opponent_{target}_allowed_l10")


def test_column_names_carry_the_target() -> None:
    """**14本で同じ列名にしない。** `feature_list` が自己説明になる（2.2.1）。"""
    per_target = {feature_keys(t)[4:] for t in TARGETS}
    assert len(per_target) == 14


def test_all_feature_keys_covers_every_target() -> None:
    keys = all_feature_keys()
    assert len(keys) == 4 + 2 * 14
    assert len(set(keys)) == len(keys)
    for target in TARGETS:
        assert set(feature_keys(target)) <= set(keys)


def test_unknown_target_is_rejected() -> None:
    """**14項目にない名前を黙って計算しない。**"""
    with pytest.raises(FeatureError, match="14項目にない"):
        feature_keys("pts")


# --- 1行は「試合 × クラブ」 ---

def test_one_row_per_club(seeded_db: sqlite3.Connection) -> None:
    """**1試合から2行できる。`is_home` が 1 と 0 の両方を取る。**

    勝敗モデルは `is_home` を持たない（常に1の定数列になる。2.2）。クラブ視点では
    特徴量になるという 2.2.1 の判断を、ここで固定する。
    """
    game_id, as_of, home, away = _game(seeded_db)
    ds = export_sqlite(seeded_db)
    home_row = build_team_rate_features(game_id, as_of, ds, home)
    away_row = build_team_rate_features(game_id, as_of, ds, away)
    assert home_row is not None and away_row is not None
    assert home_row["is_home"] == 1.0
    assert away_row["is_home"] == 0.0


def test_own_and_opponent_swap_between_sides(seeded_db: sqlite3.Connection) -> None:
    """ホーム行の `pace_own` は、アウェイ行の `pace_opp` と同じ値である。"""
    game_id, as_of, home, away = _game(seeded_db)
    ds = export_sqlite(seeded_db)
    home_row = build_team_rate_features(game_id, as_of, ds, home)
    away_row = build_team_rate_features(game_id, as_of, ds, away)
    assert home_row is not None and away_row is not None
    assert home_row["pace_own"] == pytest.approx(away_row["pace_opp"])
    assert home_row["pace_opp"] == pytest.approx(away_row["pace_own"])


def test_club_outside_the_game_is_rejected(seeded_db: sqlite3.Connection) -> None:
    """**この試合に出場しないクラブを渡したら落とす。** 黙って計算しない。"""
    game_id, as_of, home, _away = _game(seeded_db)
    other = seeded_db.execute(
        "SELECT id FROM clubs WHERE id NOT IN (?, ?) LIMIT 1",
        (home, _away),
    ).fetchone()
    with pytest.raises(FeatureError, match="出場しないクラブ"):
        build_team_rate_features(
            game_id, as_of, export_sqlite(seeded_db), str(other[0]))


def test_features_for_selects_six_columns(seeded_db: sqlite3.Connection) -> None:
    game_id, as_of, home, _ = _game(seeded_db)
    row = build_team_rate_features(game_id, as_of, export_sqlite(seeded_db), home)
    assert row is not None
    picked = features_for(row, "fg3a")
    assert list(picked) == list(feature_keys("fg3a"))


# --- 集計の仕方 ---

def test_pct_is_sum_over_sum(seeded_db: sqlite3.Connection) -> None:
    """**Σ成功数 ÷ Σ試投数。試合ごとの率を平均しない**（`off_rating` と同じ規則）。

    率の平均は、試投の少ない試合を過大に重みづける。
    """
    game_id, as_of, home, _ = _game(seeded_db)
    ds = export_sqlite(seeded_db)
    from batch.features.base import build_context
    context = build_context(game_id, as_of, ds)
    history = context.club_history(home, season_only=True).head(WINDOW)
    rows = context.stats_of(history, home)
    if rows.empty:
        pytest.skip("スタッツのある過去試合がない")

    made = float(rows["fg3m"].sum())
    attempts = float(rows["fg3a"].sum())
    expected = made / attempts
    ratio_mean = float((rows["fg3m"] / rows["fg3a"]).mean())

    actual = own_level(context, home, "fg3_pct")
    assert actual == pytest.approx(expected)
    # **二つが同じ値なら検査になっていない。** 合成シードで差が出ることを確かめる
    if abs(expected - ratio_mean) < 1e-9:
        pytest.skip("合成データでは率の平均と合計比が一致している")
    assert actual != pytest.approx(ratio_mean)


def test_count_is_a_mean(seeded_db: sqlite3.Connection) -> None:
    """カウントは直近10試合の平均。"""
    game_id, as_of, home, _ = _game(seeded_db)
    from batch.features.base import build_context
    context = build_context(game_id, as_of, export_sqlite(seeded_db))
    history = context.club_history(home, season_only=True).head(WINDOW)
    rows = context.stats_of(history, home)
    if rows.empty:
        pytest.skip("スタッツのある過去試合がない")
    assert own_level(context, home, "ast") == pytest.approx(float(rows["ast"].mean()))


def test_own_level_does_not_cross_season_boundary(seeded_db: sqlite3.Connection) -> None:
    """**シーズン境界を越えない**（`winrate_recent` と同じ規約。2.2.1）。

    2季目の序盤を狙う。当季は2試合しかないが全期間では10試合あるため、
    **越える実装なら値が変わる**。越えない実装では当季の2試合だけで平均になる。
    """
    from batch.features.base import build_context
    second = seeded_db.execute(
        "SELECT season_id FROM games GROUP BY season_id"
        " ORDER BY MIN(game_date) DESC LIMIT 1",
    ).fetchone()[0]
    row = seeded_db.execute(
        "SELECT id, tipoff_at, home_club_id FROM games WHERE season_id = ?"
        " ORDER BY game_date ASC, tipoff_at ASC LIMIT 1 OFFSET 8",
        (second,),
    ).fetchone()
    game_id, as_of, club_id = str(row[0]), datetime.fromisoformat(str(row[1])), str(row[2])
    context = build_context(game_id, as_of, export_sqlite(seeded_db))

    season_only = context.club_history(club_id, season_only=True).head(WINDOW)
    everything = context.club_history(club_id, season_only=False).head(WINDOW)
    assert len(season_only) < len(everything), (
        "シーズン境界をまたぐ条件を作れていない"
        f"（当季 {len(season_only)} / 全期間 {len(everything)}）"
    )

    inside = float(context.stats_of(season_only, club_id)["ast"].mean())
    crossing = float(context.stats_of(everything, club_id)["ast"].mean())
    assert inside != pytest.approx(crossing), "越えても値が同じなら検査にならない"
    assert own_level(context, club_id, "ast") == pytest.approx(inside)


def test_missing_level_is_nan_not_zero(seeded_db: sqlite3.Connection) -> None:
    """**既定値が設計文書にないため決め打ちで埋めない**（2.2.1）。

    旧年度は `fd`（被ファウル数）を持たないことが実在する（詳細設計 4.4）。
    列が全行 NULL のとき **NaN** になること。0 にすると「被ファウル0回のチーム」
    として学習に入る。`pace` は生きているので行は落ちない。
    """
    seeded_db.execute("UPDATE team_game_stats SET fd = NULL")
    game_id, as_of, home, _ = _game(seeded_db)
    built = build_team_rate_features(
        game_id, as_of, export_sqlite(seeded_db), home)
    assert built is not None, "pace が生きている試合を選べていない"
    assert math.isnan(built["own_fd_l10"])
    assert math.isnan(built["opponent_fd_allowed_l10"])
    # 他の項目は埋まっていること（NaN が全体に広がっていない）
    assert not math.isnan(built["own_ast_l10"])


def test_row_is_dropped_when_pace_is_missing(seeded_db: sqlite3.Connection) -> None:
    """**`pace` が欠けたら `None` を返す**（行を落とす。2.2.1）。

    シーズン最初の試合は過去のポゼッションを持たない。
    """
    row = seeded_db.execute(
        "SELECT id, tipoff_at, home_club_id FROM games WHERE status = 'FINISHED'"
        " ORDER BY game_date ASC, tipoff_at ASC LIMIT 1",
    ).fetchone()
    built = build_team_rate_features(
        str(row[0]), datetime.fromisoformat(str(row[1])),
        export_sqlite(seeded_db), str(row[2]))
    assert built is None


def test_rest_days_defaults_to_zero(seeded_db: sqlite3.Connection) -> None:
    """`rest_days_own` だけは既定値 0.0 を持つ（2.2 の `rest_days_diff` と同じ）。"""
    from batch.features.team_rate import DEFAULTS
    assert DEFAULTS == {"rest_days_own": 0.0}
