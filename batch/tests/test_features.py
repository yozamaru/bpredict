"""特徴量ごとの挙動（詳細設計 2.2 / 2.6、6.4 の境界値）。

リーク検証は `test_leakage.py` にある。ここでは定義どおりの値になるかを見る。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from batch.features.base import build_context
from batch.features.builder import DEFAULTS, build_features
from batch.features.constants import (
    REST_DAYS_CLIP,
    SEASON_REGRESSION_INITIAL,
    SHRINK_K,
)
from batch.features.dataset import export_sqlite
from batch.features.player import entry_is_official, minutes_lost
from batch.features.schedule_ctx import away_streak, rest_days, series_game_no
from batch.features.team_strength import elo, winrate_recent, winrate_season_shrunk


def _game(con: sqlite3.Connection, order: str = "ASC", offset: int = 0) -> tuple[str, datetime]:
    row = con.execute(
        f"SELECT id, tipoff_at FROM games ORDER BY game_date {order}, tipoff_at {order}"
        " LIMIT 1 OFFSET ?",
        (offset,),
    ).fetchone()
    return str(row[0]), datetime.fromisoformat(str(row[1]))


def _context(con: sqlite3.Connection, game_id: str, as_of: datetime):
    return build_context(game_id, as_of, export_sqlite(con))


def test_defaults_apply_on_the_first_game(seeded_db: sqlite3.Connection) -> None:
    """過去データがない試合では、文書が定める既定値になること（関数内で0埋めしない）。"""
    game_id, as_of = _game(seeded_db)
    features = build_features(game_id, as_of, export_sqlite(seeded_db))

    assert features["elo_home"] == DEFAULTS["elo_home"] == 1500.0
    assert features["elo_away"] == DEFAULTS["elo_away"] == 1500.0
    assert features["elo_diff"] == 0.0
    assert features["winrate_l5_diff"] == 0.0
    assert features["rest_days_diff"] == 0.0
    assert features["away_streak_away"] == 0.0


def test_winrate_l5_does_not_cross_season_boundary(seeded_db: sqlite3.Connection) -> None:
    """直近5試合の勝率がシーズン境界を越えないこと（詳細設計 6.4）。"""
    row = seeded_db.execute(
        "SELECT id, tipoff_at FROM games WHERE season_id = '2025-26-B1'"
        " ORDER BY game_date, tipoff_at LIMIT 1"
    ).fetchone()
    game_id = str(row[0])
    as_of = datetime.fromisoformat(str(row[1]))
    context = _context(seeded_db, game_id, as_of)

    # 前季の試合は大量にあるが、当季はまだ0試合
    assert len(context.club_history(context.home_club_id, season_only=False)) >= 28
    assert context.club_history(context.home_club_id, season_only=True).empty
    assert winrate_recent(context, context.home_club_id, 5) is None


def test_rest_days_is_clipped_across_season_boundary(seeded_db: sqlite3.Connection) -> None:
    """シーズン跨ぎの休養日数が上限でクリップされること（詳細設計 6.4）。"""
    row = seeded_db.execute(
        "SELECT id, tipoff_at FROM games WHERE season_id = '2025-26-B1'"
        " ORDER BY game_date, tipoff_at LIMIT 1"
    ).fetchone()
    context = _context(
        seeded_db, str(row[0]), datetime.fromisoformat(str(row[1]))
    )
    # 前季最終戦からは約5か月あるが、クリップされる
    assert rest_days(context, context.home_club_id) == REST_DAYS_CLIP


def test_series_game_no_reflects_back_to_back(seeded_db: sqlite3.Connection) -> None:
    """土日2連戦の2戦目が 2 になること（要件 6.2 の #10）。"""
    first = seeded_db.execute(
        "SELECT id, tipoff_at FROM games WHERE series_game_no = 1 ORDER BY game_date LIMIT 1"
    ).fetchone()
    second = seeded_db.execute(
        "SELECT id, tipoff_at FROM games WHERE series_game_no = 2 ORDER BY game_date LIMIT 1"
    ).fetchone()
    for row, expected in ((first, 1), (second, 2)):
        context = _context(
            seeded_db, str(row[0]), datetime.fromisoformat(str(row[1]))
        )
        assert series_game_no(context) == expected


def test_rest_days_is_zero_on_the_second_day_of_a_series(seeded_db: sqlite3.Connection) -> None:
    """連戦2戦目の休養日数が0になること（**中0日**。暦日の差ではない）。"""
    row = seeded_db.execute(
        "SELECT id, tipoff_at FROM games WHERE series_game_no = 2 ORDER BY game_date LIMIT 1"
    ).fetchone()
    context = _context(
        seeded_db, str(row[0]), datetime.fromisoformat(str(row[1]))
    )
    assert rest_days(context, context.home_club_id) == 0


def test_away_streak_counts_only_consecutive_away_games(seeded_db: sqlite3.Connection) -> None:
    """連続アウェイ試合数が、直前のホーム戦で途切れること。"""
    game_id, as_of = _game(seeded_db, order="DESC")
    context = _context(seeded_db, game_id, as_of)
    club = context.away_club_id
    history = context.club_history(club, season_only=True)
    expected = 0
    for _, row in history.iterrows():
        if int(row["is_home"]) == 1:
            break
        expected += 1
    assert away_streak(context, club) == expected


def test_elo_ignores_the_row_of_the_game_day(seeded_db: sqlite3.Connection) -> None:
    """`as_of_date < 対象試合日` であること。当日の行（終了時点の値）を読まない。

    `team_ratings` の1行はその日の終了時点の値なので、`<=` にすると
    当日の結果が混入する（詳細設計 1.4）。
    """
    game_id, as_of = _game(seeded_db, order="DESC")
    context = _context(seeded_db, game_id, as_of)
    club = context.home_club_id
    value = elo(context, club)

    same_day = seeded_db.execute(
        "SELECT elo FROM team_ratings WHERE club_id = ? AND as_of_date = ?",
        (club, context.game_date),
    ).fetchone()
    assert same_day is not None, "当日の行がないとこのテストは空振りする"
    assert value is not None
    assert abs(value - float(same_day[0])) > 1e-9


def test_shrunk_winrate_follows_the_documented_formula(seeded_db: sqlite3.Connection) -> None:
    """当季勝率の縮約が `(w + k * prior) / (n + k)` であること（詳細設計 2.6）。"""
    game_id, as_of = _game(seeded_db, order="DESC")
    context = _context(seeded_db, game_id, as_of)
    club = context.home_club_id

    history = context.club_history(club, season_only=True)
    wins = float(history["result"].dropna().sum())
    played = len(history["result"].dropna())
    previous = seeded_db.execute(
        "SELECT AVG(result) FROM team_games WHERE club_id = ? AND season_id = '2024-25-B1'",
        (club,),
    ).fetchone()[0]
    prior = 0.5 + (float(previous) - 0.5) * SEASON_REGRESSION_INITIAL
    expected = (wins + SHRINK_K * prior) / (played + SHRINK_K)

    value = winrate_season_shrunk(context, club)
    assert value is not None
    assert abs(value - expected) < 1e-9


def test_entry_is_official_is_zero_when_an_estimated_row_remains(
    seeded_db: sqlite3.Connection,
) -> None:
    """推定の行が1件でも残る試合は「確定」にしないこと（詳細設計 2.2）。"""
    game_id, as_of = _game(seeded_db, order="DESC")
    assert entry_is_official(_context(seeded_db, game_id, as_of)) == 1

    seeded_db.execute(
        "UPDATE game_entries SET source = 'ESTIMATED' WHERE game_id = ?"
        " AND player_id = (SELECT player_id FROM game_entries WHERE game_id = ? LIMIT 1)",
        (game_id, game_id),
    )
    seeded_db.commit()
    assert entry_is_official(_context(seeded_db, game_id, as_of)) == 0


def test_minutes_lost_grows_when_a_regular_is_out(seeded_db: sqlite3.Connection) -> None:
    """主力を欠場にすると、欠場者の直近平均出場時間の合計が増えること。"""
    game_id, as_of = _game(seeded_db, order="DESC")
    context = _context(seeded_db, game_id, as_of)
    club = context.home_club_id
    before = minutes_lost(context, club)
    assert before is not None

    starter = seeded_db.execute(
        "SELECT player_id FROM player_game_stats WHERE club_id = ?"
        " ORDER BY minutes DESC LIMIT 1",
        (club,),
    ).fetchone()[0]
    seeded_db.execute(
        "UPDATE game_entries SET status = 'OUT' WHERE game_id = ? AND player_id = ?",
        (game_id, starter),
    )
    seeded_db.commit()

    after = minutes_lost(_context(seeded_db, game_id, as_of), club)
    assert after is not None
    assert after > before



# --- #08 SOS（詳細設計 2.2 の `sos_diff`・検証区分） ---

def test_sos_is_the_mean_of_opponent_elo(seeded_db: sqlite3.Connection) -> None:
    """**当季に対戦した相手の、対象試合日の直前の Elo の平均**であること。

    窓と Elo の時点は既にあるものに合わせてある（詳細設計 2.2）。ここでは
    「その2つの組み合わせ」が実装どおりかを、独立に数え直して確かめる。
    """
    from batch.features.team_strength import strength_of_schedule

    game_id, as_of = _game(seeded_db, "DESC")
    context = build_context(game_id, as_of, export_sqlite(seeded_db))
    club_id = context.home_club_id

    history = context.club_history(club_id, season_only=True)
    assert not history.empty, "当季の過去試合がある試合を選べていない"
    expected = [elo(context, str(o)) for o in history["opponent_id"]]
    known = [e for e in expected if e is not None]
    assert known, "相手の Elo が1件も引けない"

    assert strength_of_schedule(context, club_id) == sum(known) / len(known)


def test_sos_uses_the_season_window(seeded_db: sqlite3.Connection) -> None:
    """**前季の相手を混ぜない**（`margin_season` と同じ窓）。"""
    from batch.features.team_strength import strength_of_schedule

    game_id, as_of = _game(seeded_db, "DESC")
    context = build_context(game_id, as_of, export_sqlite(seeded_db))
    club_id = context.home_club_id

    season = context.club_history(club_id, season_only=True)
    everything = context.club_history(club_id, season_only=False)
    assert len(everything) > len(season), "シーズンを跨ぐ履歴がある試合を選べていない"

    value = strength_of_schedule(context, club_id)
    across = [elo(context, str(o)) for o in everything["opponent_id"]]
    across_known = [e for e in across if e is not None]
    assert value != sum(across_known) / len(across_known)


def test_sos_is_none_without_a_game_this_season(seeded_db: sqlite3.Connection) -> None:
    """**開幕戦は None**（関数内で 1500 を埋めない。規約5）。"""
    from batch.features.team_strength import strength_of_schedule

    row = seeded_db.execute(
        "SELECT t.club_id, t.game_id FROM team_games t JOIN games g ON g.id = t.game_id"
        " WHERE g.status = 'FINISHED' ORDER BY g.game_date ASC LIMIT 1").fetchone()
    club_id, game_id = str(row[0]), str(row[1])
    _, as_of = _game(seeded_db, "ASC")
    context = build_context(game_id, as_of, export_sqlite(seeded_db))
    assert strength_of_schedule(context, club_id) is None


def test_sos_drops_games_whose_opponent_elo_is_unknown(
    seeded_db: sqlite3.Connection,
) -> None:
    """**相手の Elo が引けない試合は平均から落とす。** 1500 で埋めない。"""
    from batch.features.team_strength import strength_of_schedule

    game_id, as_of = _game(seeded_db, "DESC")
    context = build_context(game_id, as_of, export_sqlite(seeded_db))
    club_id = context.home_club_id
    before = strength_of_schedule(context, club_id)
    assert before is not None

    # 相手のうち1クラブの Elo を消す
    opponent = str(context.club_history(club_id, season_only=True)["opponent_id"].iloc[0])
    seeded_db.execute("DELETE FROM team_ratings WHERE club_id = ?", (opponent,))
    seeded_db.commit()

    after = strength_of_schedule(
        build_context(game_id, as_of, export_sqlite(seeded_db)), club_id)
    assert after is not None and after != before


def test_sos_uses_the_same_elo_instant_as_elo_diff(seeded_db: sqlite3.Connection) -> None:
    """**「対戦した当時の Elo」を使っていないこと。**

    同じ行列の中で Elo の時点が2種類あると、`elo_diff` との比較が何を意味するのか
    読めなくなる（詳細設計 2.2）。実装が `elo()` を通していることを固定する。
    """
    import inspect

    from batch.features import team_strength

    source = inspect.getsource(team_strength.strength_of_schedule)
    assert "elo(context," in source
    assert "as_of_date" not in source, "Elo の時点を自分で選び直している"


# --- 落とした検証区分（実装ごと削除した。詳細設計 2.2） ---

def test_dropped_verification_features_are_gone() -> None:
    """**測って落とした列が復活していないこと。**

    `efg_diff` / `ft_rate_diff`（#07）、`day_of_week` / `tipoff_hour`（#27）、
    `travel_km_diff`（#16）、`minutes_concentration_diff`（#22）。
    いずれも**実装ごと削除**した（「念のため残す」をしない）。再実装するなら、
    まず `verification/RESULTS.md` の測定を読むこと。
    """
    from batch.features.builder import FEATURE_KEYS

    for key in (
        "efg_diff", "ft_rate_diff", "day_of_week", "tipoff_hour",
        "travel_km_diff", "minutes_concentration_diff",
    ):
        assert key not in FEATURE_KEYS


def test_season_progress_is_not_implemented() -> None:
    """**分母が決まっていないため実装しない**（詳細設計 2.2）。

    「消化した試合数の割合」は `SCHEDULED` を数えることになり規約4に触れ、
    「暦の進行度」は「消化率」という語と合わない。**決めずに止める。**
    """
    from batch.features.builder import FEATURE_KEYS

    assert "season_progress" not in FEATURE_KEYS


def test_attendance_is_not_implemented() -> None:
    """**動員（#29）は3系統すべて実装しない**（詳細設計 2.2）。

    - チーム別の数（`attendance_avg_home`）は**実装して測り、落とした** —
      採用経路B の Brier が +0.000369 悪化した
    - 会場別の数（`attendance_avg_venue`）は**着手しない** — 上が効かなかったため
    - 率（`attendance_rate_home`）と収容人数（`venue_capacity`）は
      `venue_revisions.capacity` が**全件 NULL** で、入れると定数列になる

    再実装するなら、まず `verification/RESULTS.md` の測定を読むこと。
    """
    from batch.features.builder import FEATURE_KEYS

    for key in (
        "attendance_avg_home", "attendance_avg_venue",
        "attendance_rate_home", "venue_capacity",
    ):
        assert key not in FEATURE_KEYS
