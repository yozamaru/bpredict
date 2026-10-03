"""特徴量生成の前処理索引（`batch/features/prepared.py`）。

**この索引は「いつ計算するか」だけを変えるものである。** 絞り込みの意味が
変わっていないことは `test_features.py` と `test_leakage.py` が見る。ここでは
索引そのものの不変条件を固定する。

**最重要は時刻の単位である。** `pd.to_datetime` が返す解像度は入力で変わり
（pandas 3 では秒・マイクロ秒になりうる）、`astype("int64")` の値の単位も
一緒に変わる。**片方だけがナノ秒だと比較が静かに全件通る** — 実際に `as_of` の
絞り込みが効かず、1試合目に過去試合が見えていた（2026-10-02）。
目視では気づけない種類の誤りであり、テストで止める。
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pandas as pd
import pytest

from batch.features.dataset import Dataset
from batch.features.errors import FeatureError
from batch.features.prepared import NEVER, Prepared, _utc_ns, prepare


def dataset(**tables: pd.DataFrame) -> Dataset:
    """索引が読む6表。与えなかったものは空で埋める。"""
    empty = {
        "games": pd.DataFrame(columns=[
            "id", "season_id", "game_date", "finished_at",
            "home_club_id", "away_club_id",
        ]),
        "team_games": pd.DataFrame(columns=[
            "game_id", "club_id", "season_id", "finished_at",
        ]),
        "player_game_stats": pd.DataFrame(columns=["game_id", "player_id", "club_id"]),
        "game_entries": pd.DataFrame(columns=["game_id", "player_id"]),
        "team_ratings": pd.DataFrame(columns=["club_id", "as_of_date", "elo"]),
        "team_game_stats": pd.DataFrame(columns=["game_id", "club_id", "pts", "possessions"]),
    }
    return Dataset(tables={**empty, **tables})


# --- 時刻の単位（単位がずれると絞り込みが静かに全件通る） ---

@pytest.mark.parametrize("text", [
    "2024-10-05T07:05:00Z",
    "2024-10-05T07:05:00.123456Z",
    "2016-09-22T10:05:00Z",
])
def test_the_as_of_cutoff_and_the_row_times_use_the_same_unit(text: str) -> None:
    """**同じ瞬間が同じ整数になること。** 単位がずれるとここで落ちる。"""
    index = prepare(dataset(team_games=pd.DataFrame({
        "game_id": ["g"], "club_id": ["c"], "season_id": ["s"], "finished_at": [text],
    })))
    moment = datetime.fromisoformat(text)
    assert index.team_times[0] == index.as_of_ns(moment)


def test_a_row_one_second_later_is_after_the_cutoff() -> None:
    """境界の向きを固定する（`finished_at <= as_of` が同値を含むこと）。"""
    index = prepare(dataset(team_games=pd.DataFrame({
        "game_id": ["a", "b"], "club_id": ["c", "c"], "season_id": ["s", "s"],
        "finished_at": ["2024-10-05T07:05:00Z", "2024-10-05T07:05:01Z"],
    })))
    cutoff = index.as_of_ns(datetime(2024, 10, 5, 7, 5, tzinfo=UTC))
    assert list(index.team_times <= cutoff) == [True, False]


def test_a_null_time_is_never() -> None:
    """**未実施の試合は比較だけで落ちる。** どの `as_of` よりも後になる。"""
    times = _utc_ns(pd.Series([None, "2024-10-05T07:05:00Z"]))
    assert times[0] == NEVER
    assert times[1] < NEVER


def test_an_empty_table_has_no_times() -> None:
    index = prepare(dataset())
    assert index.team_times.size == 0
    assert index.player_times.size == 0


# --- 選手行の時刻は「その試合の終了時刻」である ---

def test_player_rows_take_the_time_of_their_game() -> None:
    """元の実装は `game_id.isin(終了済みの試合)` だった。同じ集合になること。"""
    index = prepare(dataset(
        games=pd.DataFrame({
            "id": ["g1", "g2"], "season_id": ["s", "s"],
            "game_date": ["2024-10-05", "2024-10-06"],
            "finished_at": ["2024-10-05T07:05:00Z", None],
            "home_club_id": ["c1", "c1"], "away_club_id": ["c2", "c2"],
        }),
        player_game_stats=pd.DataFrame({
            "game_id": ["g1", "g2", "g3"], "player_id": ["p", "p", "p"],
            "club_id": ["c1", "c1", "c1"],
        }),
    ))
    assert index.player_times[0] == index.as_of_ns(
        datetime(2024, 10, 5, 7, 5, tzinfo=UTC))
    # `finished_at` が NULL の試合の行は入らない
    assert index.player_times[1] == NEVER
    # `games` に無い試合の行も入らない（黙って 0 にしない）
    assert index.player_times[2] == NEVER


# --- 行の並び。チーム側は降順、選手側は原順序 ---

def test_the_table_itself_is_never_reordered() -> None:
    """表の行は動かさない。索引が持つのは行位置だけである。"""
    rows = pd.DataFrame({
        "game_id": ["z", "a", "m"], "club_id": ["c", "c", "c"],
        "season_id": ["s", "s", "s"],
        "finished_at": ["2024-10-05T07:05:00Z"] * 3,
    })
    index = prepare(dataset(team_games=rows))
    assert list(index.team_games["game_id"]) == ["z", "a", "m"]


def test_team_positions_are_newest_first() -> None:
    """**`club_history()` の並べ替えを索引が持つ。**

    `(finished_at, game_id)` の降順。あちらは1試合に34回呼ばれるため、
    呼び出しごとに並べ直すと全体の54%がソートになる。
    """
    index = prepare(dataset(team_games=pd.DataFrame({
        "game_id": ["a", "b", "c"], "club_id": ["x", "x", "x"],
        "season_id": ["s", "s", "s"],
        "finished_at": [
            "2024-10-05T07:05:00Z",   # 中
            "2024-10-01T07:05:00Z",   # 最古
            "2024-10-09T07:05:00Z",   # 最新
        ],
    })))
    assert list(index.club_positions["x"]) == [2, 0, 1]


def test_team_positions_break_ties_by_game_id_descending() -> None:
    """同じ時刻なら `game_id` の降順。`sort_values(..., ascending=False)` と同じ。"""
    index = prepare(dataset(team_games=pd.DataFrame({
        "game_id": ["a", "c", "b"], "club_id": ["x", "x", "x"],
        "season_id": ["s", "s", "s"],
        "finished_at": ["2024-10-05T07:05:00Z"] * 3,
    })))
    # 行位置 1 が "c"、2 が "b"、0 が "a"
    assert list(index.club_positions["x"]) == [1, 2, 0]


def test_player_positions_keep_the_original_order() -> None:
    """**選手側は並べ替えない。** `_recent_minutes` が `game_date` で並べ直し、
    同じ日の試合が複数あると入力の並びで結果が変わる。
    """
    index = prepare(dataset(
        games=pd.DataFrame({
            "id": ["g1", "g2"], "season_id": ["s", "s"],
            "game_date": ["2024-10-05", "2024-10-06"],
            "finished_at": ["2024-10-05T07:05:00Z", "2024-10-06T07:05:00Z"],
            "home_club_id": ["c1", "c1"], "away_club_id": ["c2", "c2"],
        }),
        team_games=pd.DataFrame({
            "game_id": ["g1", "g2"], "club_id": ["c1", "c1"],
            "season_id": ["s", "s"],
            "finished_at": ["2024-10-05T07:05:00Z", "2024-10-06T07:05:00Z"],
        }),
        player_game_stats=pd.DataFrame({
            "game_id": ["g2", "g1"], "player_id": ["p", "p"],
            "club_id": ["c1", "c1"],
        }),
    ))
    assert list(index.player_club_positions["c1"]) == [0, 1]


# --- クラブごとの行位置 ---

def test_positions_are_grouped_by_club_and_by_season() -> None:
    index = prepare(dataset(team_games=pd.DataFrame({
        "game_id": ["g1", "g2", "g3"],
        "club_id": ["c1", "c2", "c1"],
        "season_id": ["s1", "s1", "s2"],
        "finished_at": [
            "2024-10-01T07:05:00Z", "2024-10-05T07:05:00Z", "2024-10-09T07:05:00Z",
        ],
    })))
    # 新しい順なので g3（行位置 2）が先
    assert list(index.club_positions["c1"]) == [2, 0]
    assert list(index.club_season_positions[("c1", "s1")]) == [0]
    assert list(index.club_season_positions[("c1", "s2")]) == [2]
    assert ("c2", "s2") not in index.club_season_positions


# --- 壊れた入力はここで止める ---

def test_a_duplicate_game_id_is_rejected() -> None:
    """`games.id` は主キーである。**試合ごとに言うより前に止める。**"""
    with pytest.raises(FeatureError):
        prepare(dataset(games=pd.DataFrame({
            "id": ["g1", "g1"], "season_id": ["s", "s"],
            "game_date": ["2024-10-05", "2024-10-05"],
            "finished_at": [None, None],
            "home_club_id": ["c1", "c1"], "away_club_id": ["c2", "c2"],
        })))


# --- Elo は索引でも走査でも同じ値を返す ---

def ratings_frame() -> pd.DataFrame:
    """同じクラブに複数日、同じ日に複数クラブがある形。"""
    return pd.DataFrame(
        [
            ("c1", "2020-10-01", 1500.0),
            ("c1", "2020-10-05", 1512.0),
            ("c1", "2020-10-09", 1498.0),
            ("c2", "2020-10-05", 1488.0),
            ("c2", "2020-10-09", 1502.0),
        ],
        columns=["club_id", "as_of_date", "elo"],
    )


@pytest.mark.parametrize("club_id", ["c1", "c2", "c3"])
@pytest.mark.parametrize("game_date", [
    "2020-09-30", "2020-10-01", "2020-10-02", "2020-10-05",
    "2020-10-06", "2020-10-09", "2020-10-10",
])
def test_elo_is_the_same_with_and_without_the_index(club_id: str, game_date: str) -> None:
    """**索引を渡しても値は変わらないこと。** 境界（同日）も含めて一致する。

    `elo()` は索引があれば二分探索、無ければ走査で引く。2つの経路が食い違うと、
    探索で選んだパラメータが本番で別の値を出す。
    """
    from batch.features.base import Context
    from batch.features.team_strength import elo

    ratings = ratings_frame()
    empty = pd.DataFrame()

    def context(prepared: Prepared | None) -> Context:
        return Context(
            game_id="g",
            as_of=datetime(2020, 10, 9, tzinfo=UTC),
            game=pd.Series(dtype="object"),
            home_club_id="c1",
            away_club_id="c2",
            season_id="s1",
            game_date=game_date,
            dataset=Dataset(tables={"team_ratings": ratings}),
            finished_team_games=empty,
            entries=empty,
            prepared=prepared,
        )

    scanned = elo(context(None), club_id)
    indexed = elo(context(prepare(dataset(team_ratings=ratings))), club_id)
    assert indexed == scanned


# --- 本拠会場の索引（#15。2.1.1 の「as_of に依らない前処理」） ---

def test_primary_venue_index_matches_a_direct_read(seeded_db: sqlite3.Connection) -> None:
    """索引が、素朴に読んだ結果と一致すること。

    **索引は「いつ計算するか」だけを変える**（2.1.1）。値が変わってはならない。
    """
    from batch.features.dataset import export_sqlite
    from batch.features.prepared import prepare

    ds = export_sqlite(seeded_db)
    index = prepare(ds)

    seasons = ds.table("club_seasons")
    expected_primary = {
        (str(r["season_id"]), str(r["club_id"])): str(r["primary_venue_id"])
        for r in seasons.to_dict("records")
        if r["primary_venue_id"] is not None
    }
    assert index.primary_venues == expected_primary


def test_primary_venue_index_skips_missing_values(seeded_db: sqlite3.Connection) -> None:
    """**欠損は入れない。** 「分からない」と「値がある」を混ぜない。"""
    from batch.features.dataset import export_sqlite
    from batch.features.prepared import prepare

    seeded_db.execute("UPDATE club_seasons SET primary_venue_id = NULL")
    seeded_db.commit()
    assert prepare(export_sqlite(seeded_db)).primary_venues == {}


def test_primary_venue_index_is_empty_without_the_columns() -> None:
    """列ごと無いスナップショット（古い版）でも落ちないこと。"""
    import pandas as pd

    from batch.features.dataset import Dataset
    from batch.features.prepared import _primary_venues

    assert _primary_venues(Dataset(tables={
        "club_seasons": pd.DataFrame(columns=["club_id", "season_id"]),
    })) == {}

    # **表そのものが無い `Dataset` でも落ちない。** 手で組んだ最小のデータセットに
    # `club_seasons` は入っていない（実際に 21件のテストが `SnapshotError` で落ちた）
    assert _primary_venues(Dataset(tables={})) == {}
