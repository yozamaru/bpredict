"""リーク検証（最重要）。

**検証の向きは「DBを撹乱する」。「`as_of` をずらす」ではない**（基本設計 8.2）。
`as_of` を後ろにずらすと参照できる過去試合が増えるため、正しい実装なら値は変わるのが
正常である。「変わらないこと」を assert するテストは、正しい実装を落とし、`as_of` を
一切見ない実装だけを通す。

`test_accuracy_within_plausible_band`（`baseline < accuracy < 0.85`）はモデルの評価指標を
必要とするため、工程8（学習と評価）で追加する。ここには特徴量生成に対する検証を置く。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from batch.features.base import build_context
from batch.features.builder import FEATURE_KEYS, build_features
from batch.features.dataset import Dataset, export_sqlite

TOLERANCE = 1e-9


def _target(con: sqlite3.Connection, offset_from_end: int = 0) -> tuple[str, datetime]:
    """終盤の試合を1件選ぶ（過去データが十分に積まれている試合を使う）。"""
    rows = con.execute(
        "SELECT id, tipoff_at FROM games ORDER BY game_date DESC, tipoff_at DESC LIMIT 1 OFFSET ?",
        (offset_from_end,),
    ).fetchall()
    game_id, tipoff = rows[0]
    return str(game_id), datetime.fromisoformat(str(tipoff))


def _features(con: sqlite3.Connection, game_id: str, as_of: datetime) -> dict[str, float]:
    return build_features(game_id, as_of, export_sqlite(con))


def _assert_same(first: dict[str, float], second: dict[str, float]) -> None:
    assert set(first) == set(second)
    differing = {
        key: (first[key], second[key])
        for key in first
        if abs(first[key] - second[key]) > TOLERANCE
    }
    assert not differing, f"撹乱で変化した特徴量がある（リーク）: {differing}"


def test_target_game_mutation_does_not_change_features(seeded_db: sqlite3.Connection) -> None:
    """**本命。** `as_of` 固定のまま対象試合を撹乱しても、全キーが不変であること。"""
    game_id, as_of = _target(seeded_db)
    before = _features(seeded_db, game_id, as_of)

    seeded_db.execute(
        "UPDATE games SET home_score = 200, away_score = 0, attendance = 99999 WHERE id = ?",
        (game_id,),
    )
    seeded_db.execute(
        "UPDATE team_game_stats SET pts = 200, fg2m = 100, fg2a = 100 WHERE game_id = ?",
        (game_id,),
    )
    seeded_db.execute(
        "UPDATE player_game_stats SET pts = 40, minutes = 40 WHERE game_id = ?", (game_id,)
    )
    seeded_db.execute(
        "UPDATE team_games SET result = 1, margin = 200 WHERE game_id = ?", (game_id,)
    )
    seeded_db.commit()

    _assert_same(before, _features(seeded_db, game_id, as_of))


def test_future_game_mutation_does_not_change_features(seeded_db: sqlite3.Connection) -> None:
    """`as_of` 以降に終了した試合を撹乱しても不変であること。"""
    game_id, as_of = _target(seeded_db, offset_from_end=30)
    before = _features(seeded_db, game_id, as_of)

    boundary = as_of.isoformat(timespec="seconds").replace("+00:00", "Z")
    changed = seeded_db.execute(
        "UPDATE team_games SET result = 1, margin = 99 WHERE finished_at > ?", (boundary,)
    ).rowcount
    seeded_db.execute(
        "UPDATE games SET home_score = 199, away_score = 1 WHERE finished_at > ?", (boundary,)
    )
    seeded_db.commit()
    assert changed > 0, "撹乱対象がない。テストが空振りしている"

    _assert_same(before, _features(seeded_db, game_id, as_of))


def test_as_of_is_actually_applied(seeded_db: sqlite3.Connection) -> None:
    """**陽性確認。** `as_of` を前にずらすと結果依存の特徴量が変化すること。

    これが通らない実装は `as_of` を見ていない。撹乱テストだけでは検出できない。
    """
    game_id, as_of = _target(seeded_db)
    now = _features(seeded_db, game_id, as_of)
    past = _features(seeded_db, game_id, as_of - timedelta(days=30))

    changed = [key for key in now if abs(now[key] - past[key]) > TOLERANCE]
    assert any(
        key.startswith(("elo_", "winrate_", "margin_")) for key in changed
    ), f"as_of を30日戻しても結果依存の特徴量が変わらない: {changed}"


def test_features_exclude_unfinished_games(seeded_db: sqlite3.Connection) -> None:
    """`SCHEDULED` の試合が集計に入らないこと。"""
    game_id, as_of = _target(seeded_db)
    before = _features(seeded_db, game_id, as_of)

    # 過去の試合を「未実施」に戻すと、集計から外れて値が変わるはず（陽性確認）
    victim = seeded_db.execute(
        "SELECT game_id FROM team_games WHERE club_id = (SELECT home_club_id FROM games WHERE id = ?)"
        " AND finished_at <= (SELECT tipoff_at FROM games WHERE id = ?)"
        " ORDER BY finished_at DESC LIMIT 1",
        (game_id, game_id),
    ).fetchone()[0]
    seeded_db.execute(
        "UPDATE games SET status = 'SCHEDULED', finished_at = NULL, home_score = NULL,"
        " away_score = NULL WHERE id = ?",
        (victim,),
    )
    seeded_db.execute(
        "UPDATE team_games SET finished_at = NULL, result = NULL, margin = NULL WHERE game_id = ?",
        (victim,),
    )
    seeded_db.commit()

    after = _features(seeded_db, game_id, as_of)
    assert any(
        abs(before[key] - after[key]) > TOLERANCE for key in before
    ), "終了済みだった試合を未実施に変えても値が変わらない。finished_at を見ていない"


def test_features_exclude_in_progress_game(seeded_db: sqlite3.Connection) -> None:
    """開始済み・未終了の試合が集計に入らないこと。

    `tipoff_at <= as_of < finished_at` の試合を作る。`tipoff_at` で絞る実装だと
    この試合が確定情報として混入する。
    """
    game_id, as_of = _target(seeded_db)
    baseline = _features(seeded_db, game_id, as_of)

    in_progress = seeded_db.execute(
        "SELECT id FROM games WHERE finished_at > ? ORDER BY tipoff_at LIMIT 1",
        (as_of.isoformat(timespec="seconds").replace("+00:00", "Z"),),
    ).fetchone()
    if in_progress is None:
        pytest.skip("as_of 以降の試合がない")
    started = (as_of - timedelta(hours=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
    ends = (as_of + timedelta(hours=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
    seeded_db.execute(
        "UPDATE games SET tipoff_at = ?, finished_at = ? WHERE id = ?",
        (started, ends, in_progress[0]),
    )
    seeded_db.execute(
        "UPDATE team_games SET finished_at = ? WHERE game_id = ?", (ends, in_progress[0])
    )
    seeded_db.commit()

    _assert_same(baseline, _features(seeded_db, game_id, as_of))


def test_target_game_is_excluded_even_if_finished_before_as_of(
    seeded_db: sqlite3.Connection,
) -> None:
    """対象試合自身を明示的に除外していること。

    `as_of` の比較だけに頼ると、終了時刻が `as_of` より前に記録されている
    （実測値が取れた場合や、データの誤り）試合で自分自身を参照してしまう。
    """
    game_id, as_of = _target(seeded_db)
    context = build_context(game_id, as_of, export_sqlite(seeded_db))
    assert game_id not in set(context.finished_team_games["game_id"])

    early = (as_of - timedelta(hours=3)).isoformat(timespec="seconds").replace("+00:00", "Z")
    seeded_db.execute("UPDATE games SET finished_at = ? WHERE id = ?", (early, game_id))
    seeded_db.execute(
        "UPDATE team_games SET finished_at = ? WHERE game_id = ?", (early, game_id)
    )
    seeded_db.commit()

    context = build_context(game_id, as_of, export_sqlite(seeded_db))
    assert game_id not in set(context.finished_team_games["game_id"])


def test_current_affiliation_is_not_used_for_player_features(
    seeded_db: sqlite3.Connection,
) -> None:
    """チーム所属の判定に「現在の所属」を使っていないこと（詳細設計 2.1 の規約6）。

    移籍を起こしても、過去の出場実績はそのクラブに残るのが正しい。
    現在の所属で判定する実装だと、過去の出場時間ごと新チームへ移動する。
    **`as_of` 規約には違反しないため、撹乱テストでは検出されない。**
    """
    game_id, as_of = _target(seeded_db)
    before = _features(seeded_db, game_id, as_of)

    home_club = seeded_db.execute(
        "SELECT home_club_id FROM games WHERE id = ?", (game_id,)
    ).fetchone()[0]
    other_club = seeded_db.execute(
        "SELECT id FROM clubs WHERE id != ? ORDER BY id LIMIT 1", (home_club,)
    ).fetchone()[0]
    moved = seeded_db.execute(
        "UPDATE player_seasons SET club_id = ? WHERE club_id = ?", (other_club, home_club)
    ).rowcount
    assert moved > 0
    seeded_db.commit()

    _assert_same(before, _features(seeded_db, game_id, as_of))


def test_backtest_uses_production_data_as_of(seeded_db: sqlite3.Connection) -> None:
    """同じ `as_of` と同じデータ鮮度なら、再構築した特徴量が一致すること。

    本番の日次推論は最大7日前の情報で予測するが、バックテストでは全試合が
    揃っている。`data_as_of` を記録しないと、この差（検証スコアの体系的な
    過大評価）を後から検証できない（要件 6.3）。
    """
    game_id, as_of = _target(seeded_db)
    full = export_sqlite(seeded_db)
    stored = build_features(game_id, as_of, full)

    # 推論時点のデータ鮮度を再現する（その時刻までに終了していた試合だけを残す）
    data_as_of = as_of - timedelta(days=7)
    truncated = _truncate(full, data_as_of)
    stale = build_features(game_id, as_of, truncated)

    assert stale.keys() == stored.keys()
    assert any(
        abs(stale[key] - stored[key]) > TOLERANCE for key in stored
    ), "データ鮮度を7日戻しても値が変わらない。data_as_of を記録する意味がなくなる"
    assert build_features(game_id, as_of, _truncate(full, data_as_of)) == stale


def _truncate(dataset: Dataset, data_as_of: datetime) -> Dataset:
    """`data_as_of` 時点の DB の状態を再現する。

    **未終了の試合の行は消さない。** その時点では `SCHEDULED` として存在しており、
    結果だけが入っていない。対象試合そのものもこれに該当する。
    """
    import pandas as pd

    games = dataset.table("games").copy()
    finished = pd.to_datetime(games["finished_at"], utc=True, format="ISO8601")
    unfinished = finished.isna() | (finished > data_as_of)
    games.loc[unfinished, ["finished_at", "home_score", "away_score", "attendance"]] = None
    games.loc[unfinished, "status"] = "SCHEDULED"
    pending = set(games[unfinished]["id"])

    team_games = dataset.table("team_games").copy()
    rows = team_games["game_id"].isin(pending)
    team_games.loc[rows, ["finished_at", "result", "margin"]] = None

    tables = dict(dataset.tables)
    tables["games"] = games
    tables["team_games"] = team_games
    for name in ("team_game_stats", "player_game_stats"):
        frame = tables[name]
        tables[name] = frame[~frame["game_id"].isin(pending)]
    ratings = tables["team_ratings"]
    tables["team_ratings"] = ratings[ratings["as_of_date"] <= data_as_of.date().isoformat()]
    return Dataset(tables=tables)


def test_feature_keys_are_fixed(seeded_db: sqlite3.Connection) -> None:
    """キーの集合が定義と一致すること。列が増減したら気づけるようにする。"""
    game_id, as_of = _target(seeded_db)
    assert tuple(_features(seeded_db, game_id, as_of)) == FEATURE_KEYS


def test_mutation_trial_detects_an_intentional_leak(seeded_db: sqlite3.Connection) -> None:
    """**テストのテスト。** 意図的にリークさせた実装で撹乱テストが落ちること。

    リークテスト自体が壊れている（常に通る）場合、本番のリークを検出できない。
    """
    game_id, as_of = _target(seeded_db)

    def leaky(connection: sqlite3.Connection) -> dict[str, float]:
        """対象試合の得点差を特徴量に混ぜた実装。"""
        base = _features(connection, game_id, as_of)
        row = connection.execute(
            "SELECT home_score, away_score FROM games WHERE id = ?", (game_id,)
        ).fetchone()
        return {**base, "elo_diff": base["elo_diff"] + float(row[0] - row[1])}

    before = leaky(seeded_db)
    seeded_db.execute(
        "UPDATE games SET home_score = 200, away_score = 0 WHERE id = ?", (game_id,)
    )
    seeded_db.commit()
    after = leaky(seeded_db)

    with pytest.raises(AssertionError):
        _assert_same(before, after)
