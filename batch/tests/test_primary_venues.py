"""本拠会場の導出と #15（詳細設計 1.2 / 4.11）。

| テスト | どの規約か |
|---|---|
| `test_mode_is_the_most_used_home_venue` | ホーム試合の最頻会場 |
| `test_unplayed_games_are_not_counted` | 未実施の試合を数えない（規約4） |
| `test_tie_takes_the_earlier_venue` | 同数なら早く使った方（同日なら `venue_id` 昇順） |
| `test_no_share_threshold` | 占有率の下限を設けない |
| `test_manual_rows_survive_derivation` | 手入力の行を導出で置き換えない |
| `test_feature_reads_the_master_not_the_column` | 特徴量は `games.is_primary_venue` を読まない |
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from batch.features.base import build_context
from batch.features.dataset import export_sqlite
from batch.features.venue import is_primary_venue as feature_is_primary
from batch.masters.primary_venues import (
    CSV_COLUMNS,
    PrimaryVenue,
    PrimaryVenueError,
    derive,
    is_primary_venue,
    load_csv,
    merge,
    primary_of,
    write_csv,
)

COLUMNS = ("status", "season_id", "home_club_id", "venue_id", "game_date")


def games(rows: list[dict[str, object]]) -> pd.DataFrame:
    base = {
        "status": "FINISHED", "season_id": "s1", "home_club_id": "c1",
        "venue_id": "v1", "game_date": "2026-01-01",
    }
    # **空でも列は持たせる。** 列の有無の検査と、行が無いことは別の話である
    return pd.DataFrame([{**base, **r} for r in rows], columns=list(COLUMNS))


# --- 導出 ---

def test_mode_is_the_most_used_home_venue() -> None:
    result = derive(games([
        {"venue_id": "v1", "game_date": "2026-01-01"},
        {"venue_id": "v1", "game_date": "2026-01-02"},
        {"venue_id": "v2", "game_date": "2026-01-03"},
    ]))
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.venue_id == "v1"
    assert row.games == 2
    assert row.share == pytest.approx(2 / 3)


def test_away_games_are_not_counted() -> None:
    """**ホーム試合だけを数える。** アウェイの会場は本拠ではない。"""
    frame = games([
        {"venue_id": "v1", "game_date": "2026-01-01"},
        {"venue_id": "v9", "game_date": "2026-01-02", "home_club_id": "c2"},
        {"venue_id": "v9", "game_date": "2026-01-03", "home_club_id": "c2"},
    ])
    result = derive(frame)
    by_club = {r.club_id: r.venue_id for r in result.rows}
    assert by_club == {"c1": "v1", "c2": "v9"}


def test_unplayed_games_are_not_counted() -> None:
    """**未実施の試合を含めない**（特徴量の規約4 / 詳細設計 1.2）。

    日程が公表済みでも、そこから数えるのは「未来の試合を見る」ことになる。
    """
    result = derive(games([
        {"venue_id": "v1", "game_date": "2026-01-01"},
        {"venue_id": "v2", "game_date": "2026-02-01", "status": "SCHEDULED"},
        {"venue_id": "v2", "game_date": "2026-02-02", "status": "SCHEDULED"},
        {"venue_id": "v2", "game_date": "2026-02-03", "status": "SCHEDULED"},
    ]))
    assert [r.venue_id for r in result.rows] == ["v1"]


def test_games_without_a_venue_are_not_counted() -> None:
    result = derive(games([
        {"venue_id": None, "game_date": "2026-01-01"},
        {"venue_id": None, "game_date": "2026-01-02"},
        {"venue_id": "v2", "game_date": "2026-01-03"},
    ]))
    assert [r.venue_id for r in result.rows] == ["v2"]
    assert result.rows[0].share == pytest.approx(1.0)


def test_seasons_are_separate() -> None:
    """**シーズンごとに決める。** 本拠はシーズンで変わる（実測で 195組中28件）。"""
    result = derive(games([
        {"venue_id": "v1", "game_date": "2026-01-01", "season_id": "s1"},
        {"venue_id": "v2", "game_date": "2027-01-01", "season_id": "s2"},
    ]))
    assert {(r.season_id, r.venue_id) for r in result.rows} == {("s1", "v1"), ("s2", "v2")}


def test_tie_takes_the_earlier_venue() -> None:
    """**同数なら早く使った方**（詳細設計 1.2）。

    実データでは225組すべてで同数が起きないが、**決めていない規則を実装に残さない**。
    同数が起きたことは報告する。
    """
    result = derive(games([
        {"venue_id": "v2", "game_date": "2026-01-01"},
        {"venue_id": "v1", "game_date": "2026-01-02"},
    ]))
    assert result.rows[0].venue_id == "v2", "早く使った方を採る"
    assert result.ties == [("s1", "c1")], "同数を報告する"


def test_tie_on_the_same_day_takes_the_smaller_id() -> None:
    """同日なら `venue_id` の昇順。**実行ごとに値が変わらないこと**が要点。"""
    frame = games([
        {"venue_id": "v9", "game_date": "2026-01-01"},
        {"venue_id": "v1", "game_date": "2026-01-01"},
    ])
    first = derive(frame).rows[0].venue_id
    # 入力の並びを変えても同じ結果になること
    second = derive(frame.iloc[::-1].reset_index(drop=True)).rows[0].venue_id
    assert first == second == "v1"


def test_no_share_threshold() -> None:
    """**占有率の下限を設けない**（詳細設計 1.2）。

    閾値は設計文書に根拠のない定数になる。下限なしで最頻会場を採れば、占有率の
    低いクラブでは `is_primary_venue = 0` の試合が多くなるだけである。
    """
    rows: list[dict[str, object]] = [{"venue_id": "v1", "game_date": "2026-01-01"}]
    rows += [{"venue_id": f"v{i}", "game_date": f"2026-02-{i:02d}"} for i in range(2, 10)]
    result = derive(games(rows))
    assert len(result.rows) == 1
    assert result.rows[0].share < 0.2, "占有率が低くても行を作る"


def test_missing_columns_are_rejected() -> None:
    with pytest.raises(PrimaryVenueError, match="必要な列がない"):
        derive(pd.DataFrame({"status": ["FINISHED"]}))


def test_no_rows_when_there_are_no_home_games() -> None:
    assert derive(games([])).rows == []


# --- 手入力との合流 ---

def test_manual_rows_survive_derivation() -> None:
    """**手入力の行を導出で置き換えない**（詳細設計 4.11）。

    消すと、開幕前に調べて入れた値がシーズン途中で勝手に変わる。
    """
    derived = derive(games([
        {"venue_id": "v1", "game_date": "2026-01-01"},
        {"venue_id": "v1", "game_date": "2026-01-02"},
    ]))
    manual = PrimaryVenue("s1", "c1", "v-manual", 0, 0.0, "https://example.test/")
    out = merge(derived, [manual])
    assert [r.venue_id for r in out.rows] == ["v-manual"]
    assert out.conflicts == [("s1", "c1", "v-manual", "v1")], "食い違いを報告する"


def test_manual_rows_for_seasons_without_games_are_kept() -> None:
    """**当季（まだ試合がない）の行が残ること。** これが手入力の主な用途である。"""
    derived = derive(games([{"venue_id": "v1", "game_date": "2026-01-01"}]))
    manual = PrimaryVenue("s2", "c1", "v-new", 0, 0.0, "https://example.test/")
    out = merge(derived, [manual])
    assert {(r.season_id, r.venue_id) for r in out.rows} == {("s1", "v1"), ("s2", "v-new")}


def test_derived_rows_are_not_treated_as_manual() -> None:
    """`source` が空なら導出。**導出した行は次の実行で更新される。**"""
    derived = derive(games([{"venue_id": "v2", "game_date": "2026-01-01"}]))
    previous = PrimaryVenue("s1", "c1", "v1", 5, 0.9, "")
    out = merge(derived, [previous])
    assert [r.venue_id for r in out.rows] == ["v2"]
    assert out.conflicts == []


# --- CSV ---

def test_csv_round_trip(tmp_path: Path) -> None:
    rows = [
        PrimaryVenue("s1", "c1", "v1", 24, 0.8, ""),
        PrimaryVenue("s2", "c1", "v2", 0, 0.0, "https://example.test/"),
    ]
    path = tmp_path / "pv.csv"
    write_csv(rows, path)
    assert path.read_text(encoding="utf-8").splitlines()[0] == ",".join(CSV_COLUMNS)
    back = load_csv(path)
    assert [(r.season_id, r.venue_id, r.is_manual) for r in back] == [
        ("s1", "v1", False), ("s2", "v2", True)]


def test_csv_keeps_the_share(tmp_path: Path) -> None:
    """**判断の材料を捨てない**（詳細設計 4.11）。占有率と試合数を残す。"""
    path = tmp_path / "pv.csv"
    write_csv([PrimaryVenue("s1", "c1", "v1", 11, 0.3667, "")], path)
    back = load_csv(path)
    assert back[0].games == 11
    assert back[0].share == pytest.approx(0.3667)


def test_csv_with_wrong_columns_is_rejected(tmp_path: Path) -> None:
    """**黙って補わない。** 列が違えば落とす。"""
    path = tmp_path / "pv.csv"
    path.write_text("season_id,club_id\ns1,c1\n", encoding="utf-8")
    with pytest.raises(PrimaryVenueError, match="列が"):
        load_csv(path)


def test_missing_csv_is_empty(tmp_path: Path) -> None:
    assert load_csv(tmp_path / "none.csv") == []


# --- is_primary_venue の導出 ---

def test_unknown_primary_stays_one() -> None:
    """**本拠が分からないクラブ×シーズンは 1 のまま**（詳細設計 1.2）。

    「本拠が分からない」と「代替会場である」は違う。
    """
    frame = games([
        {"venue_id": "v1", "game_date": "2026-01-01"},
        {"venue_id": "v2", "game_date": "2026-01-02", "season_id": "s9"},
    ])
    values = is_primary_venue(frame, {("s1", "c1"): "v1"})
    assert values.tolist() == [1, 1], "s9 は対応表にないので 1"


def test_alternate_venue_is_zero() -> None:
    frame = games([
        {"venue_id": "v1", "game_date": "2026-01-01"},
        {"venue_id": "v2", "game_date": "2026-01-02"},
    ])
    assert is_primary_venue(frame, {("s1", "c1"): "v1"}).tolist() == [1, 0]


def test_primary_of_builds_the_lookup() -> None:
    rows = [PrimaryVenue("s1", "c1", "v1", 1, 1.0, "")]
    assert primary_of(rows) == {("s1", "c1"): "v1"}


# --- 特徴量 #15 ---

def _target(con: sqlite3.Connection) -> tuple[str, datetime]:
    row = con.execute(
        "SELECT id, tipoff_at FROM games WHERE status = 'FINISHED'"
        " ORDER BY game_date DESC LIMIT 1").fetchone()
    return str(row[0]), datetime.fromisoformat(str(row[1]))


def test_feature_is_none_when_the_master_is_empty(seeded_db: sqlite3.Connection) -> None:
    """**マスタが NULL なら None**（関数内で埋めない。規約5）。

    本番の `club_seasons.primary_venue_id` は全件 NULL である（2026-10-03 時点）。
    その状態で #15 を `FEATURE_KEYS` に入れると**定数列**になり、採用ゲートの
    `constant_columns` に当たる。だから**マスタを投入してから列に足す**。
    """
    seeded_db.execute("UPDATE club_seasons SET primary_venue_id = NULL")
    seeded_db.commit()
    game_id, as_of = _target(seeded_db)
    context = build_context(game_id, as_of, export_sqlite(seeded_db))
    assert feature_is_primary(context) is None


def test_feature_reads_the_master_not_the_column(seeded_db: sqlite3.Connection) -> None:
    """**`games.is_primary_venue` を読まない**（詳細設計 1.2）。

    列を 0 にしてもマスタが本拠と言えば 1 を返す。逆も確かめる。
    """
    game_id, as_of = _target(seeded_db)
    row = seeded_db.execute(
        "SELECT home_club_id, season_id, venue_id FROM games WHERE id = ?",
        (game_id,)).fetchone()
    club_id, season_id, venue_id = str(row[0]), str(row[1]), str(row[2])

    seeded_db.execute("UPDATE games SET is_primary_venue = 0 WHERE id = ?", (game_id,))
    seeded_db.execute(
        "UPDATE club_seasons SET primary_venue_id = ? WHERE club_id = ? AND season_id = ?",
        (venue_id, club_id, season_id))
    seeded_db.commit()
    context = build_context(game_id, as_of, export_sqlite(seeded_db))
    assert feature_is_primary(context) == 1.0, "列が 0 でもマスタに従う"

    other = seeded_db.execute(
        "SELECT id FROM venues WHERE id <> ? LIMIT 1", (venue_id,)).fetchone()
    assert other is not None, "別の会場が1件もない（検査の条件を作れていない）"
    seeded_db.execute(
        "UPDATE club_seasons SET primary_venue_id = ?"
        " WHERE club_id = ? AND season_id = ?", (str(other[0]), club_id, season_id))
    seeded_db.execute("UPDATE games SET is_primary_venue = 1 WHERE id = ?", (game_id,))
    seeded_db.commit()
    context = build_context(game_id, as_of, export_sqlite(seeded_db))
    assert feature_is_primary(context) == 0.0, "列が 1 でもマスタに従う"
