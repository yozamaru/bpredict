"""リーク検証（最重要）。

**検証の向きは「DBを撹乱する」。「`as_of` をずらす」ではない**（基本設計 8.2）。
`as_of` を後ろにずらすと参照できる過去試合が増えるため、正しい実装なら値は変わるのが
正常である。「変わらないこと」を assert するテストは、正しい実装を落とし、`as_of` を
一切見ない実装だけを通す。

`test_accuracy_within_plausible_band`（`baseline < accuracy < 0.85`）はモデルの評価指標を
必要とするため、工程8（学習と評価）で追加する。ここには特徴量生成に対する検証を置く。
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta

import pytest

from batch.features.base import Context, build_context
from batch.features.builder import FEATURE_KEYS, build_features
from batch.features.dataset import Dataset, export_sqlite
from batch.features.team_rate import all_feature_keys, build_team_rate_features

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
    # **ボックススコアも撹乱する。** `ortg_diff` ほかの検証区分は
    # `team_game_stats` を読む（詳細設計 2.2）。`team_games` だけを撹乱していると、
    # そちらの経路でのリークをこのテストが見逃す
    stats = seeded_db.execute(
        # 成功数も一緒に動かす。**DDL の CHECK（成功数 ≤ 試投数）を満たすこと** —
        # 試投数だけを下げると制約違反で落ち、リークの有無が見えなくなる
        "UPDATE team_game_stats SET pts = 199, possessions = 200,"
        " fg2m = 1, fg2a = 1, fg3m = 1, fg3a = 1, ftm = 1, fta = 1,"
        " oreb = 99, dreb = 99, tov = 99 WHERE game_id IN"
        " (SELECT id FROM games WHERE finished_at > ?)",
        (boundary,),
    ).rowcount
    seeded_db.commit()
    assert changed > 0, "撹乱対象がない。テストが空振りしている"
    assert stats > 0, "ボックススコアの撹乱対象がない。テストが空振りしている"

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
    # **最終試合を選ばない。** `_target()` の既定（`offset_from_end=0`）は最後の試合で、
    # 後続の試合が存在しない。**この検査は対象試合自身を撹乱して通っていた** —
    # 自分を除外した途端に撹乱対象が無くなり、本来の「他の進行中の試合」を
    # 一度も試していなかったことが分かった（2026-10-02）。
    game_id, as_of = _target(seeded_db, offset_from_end=30)
    baseline = _features(seeded_db, game_id, as_of)

    # **対象試合自身を選ばない。** 対象試合は `finished_at > as_of` を満たす
    # （自分の開始時刻の時点では終わっていない）ため、この条件だけだと自分が選ばれる。
    # すると撹乱が対象試合の `tipoff_at` を書き換えることになり、
    # `tipoff_hour`（日程の属性。規約3 が禁じるのは対象試合の**スタッツ**である）が
    # 追従して「リーク」と判定された。**本番では `as_of` = 対象試合の `tipoff_at`
    # であり、片方だけが動く状態は起こらない。**
    in_progress = seeded_db.execute(
        "SELECT id FROM games WHERE finished_at > ? AND id <> ? ORDER BY tipoff_at LIMIT 1",
        (as_of.isoformat(timespec="seconds").replace("+00:00", "Z"), game_id),
    ).fetchone()
    # **skip にしない。** 撹乱対象が無いまま通ると、この検査は黙って空振りする。
    assert in_progress is not None, "撹乱対象がない。テストが空振りしている"
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


def _assert_target_absent_everywhere(
    context: Context, game_id: str, where: str,
) -> None:
    """**特徴量が実際に読むすべての入口**に対象試合が現れないこと。

    `finished_team_games` だけを見ていると穴が残る。2.1.1 の索引を入れた時点で
    `club_history` / `player_history` が**それぞれ独立に絞り込みを持つ**ように
    なっており、片方だけを検査すると、もう片方から除外を外しても1件も落ちない
    （2026-10-03 の変異試験で実際にそうなった）。
    """
    assert game_id not in set(context.finished_team_games["game_id"]), (
        f"{where}: finished_team_games に対象試合がある")
    for club_id in (context.home_club_id, context.away_club_id):
        for season_only in (True, False):
            history = context.club_history(club_id, season_only=season_only)
            assert game_id not in set(history["game_id"].astype(str)), (
                f"{where}: club_history({club_id}, season_only={season_only})"
                " に対象試合がある")
            players = context.player_history(club_id, season_only=season_only)
            assert game_id not in set(players["game_id"].astype(str)), (
                f"{where}: player_history({club_id}, season_only={season_only})"
                " に対象試合がある")
    stats = context.finished_player_stats
    assert game_id not in set(stats["game_id"].astype(str)), (
        f"{where}: finished_player_stats に対象試合がある")


def test_target_game_is_excluded_even_if_finished_before_as_of(
    seeded_db: sqlite3.Connection,
) -> None:
    """対象試合自身を明示的に除外していること。

    `as_of` の比較だけに頼ると、終了時刻が `as_of` より前に記録されている
    （実測値が取れた場合や、データの誤り）試合で自分自身を参照してしまう。

    **検査は入口ごとに行う**（`_assert_target_absent_everywhere`）。
    """
    game_id, as_of = _target(seeded_db)
    _assert_target_absent_everywhere(
        build_context(game_id, as_of, export_sqlite(seeded_db)),
        game_id, "finished_at が as_of より後")

    early = (as_of - timedelta(hours=3)).isoformat(timespec="seconds").replace("+00:00", "Z")
    seeded_db.execute("UPDATE games SET finished_at = ? WHERE id = ?", (early, game_id))
    seeded_db.execute(
        "UPDATE team_games SET finished_at = ? WHERE game_id = ?", (early, game_id)
    )
    seeded_db.commit()

    _assert_target_absent_everywhere(
        build_context(game_id, as_of, export_sqlite(seeded_db)),
        game_id, "finished_at を as_of より前に書き換えた")


def test_target_game_mutation_is_invisible_when_it_finished_before_as_of(
    seeded_db: sqlite3.Connection,
) -> None:
    """**除外が効いていることを、値で確かめる。**

    上のテストは「行が入っていない」ことを見るが、それだけでは
    `finished_team_games` の形を変えずに別経路で読む実装を通す。ここでは
    対象試合の `finished_at` を `as_of` より前にしたうえで**スタッツを撹乱**し、
    勝敗モデルと TeamRate の両方の特徴量が動かないことを見る。
    """
    game_id, as_of = _target(seeded_db)
    early = (as_of - timedelta(hours=3)).isoformat(timespec="seconds").replace("+00:00", "Z")
    seeded_db.execute("UPDATE games SET finished_at = ? WHERE id = ?", (early, game_id))
    seeded_db.execute(
        "UPDATE team_games SET finished_at = ? WHERE game_id = ?", (early, game_id))
    seeded_db.commit()

    club_id = str(seeded_db.execute(
        "SELECT home_club_id FROM games WHERE id = ?", (game_id,)).fetchone()[0])
    before_game = _features(seeded_db, game_id, as_of)
    before_rate = _team_rate_features(seeded_db, game_id, as_of, club_id)

    seeded_db.execute(
        "UPDATE games SET home_score = 200, away_score = 0 WHERE id = ?", (game_id,))
    seeded_db.execute(
        "UPDATE team_games SET result = 1, margin = 200 WHERE game_id = ?", (game_id,))
    seeded_db.execute(
        "UPDATE team_game_stats SET pts = 200, possessions = 200,"
        " fg2m = 60, fg2a = 60, fg3m = 20, fg3a = 20, ftm = 20, fta = 20,"
        " oreb = 99, dreb = 99, ast = 99, tov = 99, stl = 99, blk = 99,"
        " pf = 6, fd = 99 WHERE game_id = ?", (game_id,))
    seeded_db.execute(
        "UPDATE player_game_stats SET pts = 40, minutes = 40 WHERE game_id = ?",
        (game_id,))
    seeded_db.commit()

    _assert_same(before_game, _features(seeded_db, game_id, as_of))
    _assert_same_allowing_nan(
        before_rate, _team_rate_features(seeded_db, game_id, as_of, club_id))


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


# --- TeamRate の特徴量（詳細設計 2.2.1） ---
#
# **勝敗モデルと同じ検証を、クラブ視点の行にも当てる。** 列が別ならリークの経路も
# 別である。ここを省くと「勝敗は守られているが目標値は漏れている」状態になり、
# 整合化を通して個人スタッツまで汚染される。


def _team_rate_target(
    con: sqlite3.Connection, offset_from_end: int = 0,
) -> tuple[str, datetime, str]:
    """終盤の終了済み試合と、そのホームクラブ。"""
    row = con.execute(
        "SELECT id, tipoff_at, home_club_id FROM games WHERE status = 'FINISHED'"
        " ORDER BY game_date DESC, tipoff_at DESC LIMIT 1 OFFSET ?",
        (offset_from_end,),
    ).fetchone()
    return str(row[0]), datetime.fromisoformat(str(row[1])), str(row[2])


def _team_rate_features(
    con: sqlite3.Connection, game_id: str, as_of: datetime, club_id: str,
) -> dict[str, float]:
    row = build_team_rate_features(game_id, as_of, export_sqlite(con), club_id)
    assert row is not None, "pace が欠けて行が落ちた。過去データのある試合を選ぶ"
    return row


def _assert_same_allowing_nan(
    first: dict[str, float], second: dict[str, float],
) -> None:
    """NaN を許す比較。**NaN どうしは等しいとみなす**（欠損は撹乱で動かない）。"""
    assert set(first) == set(second)
    differing = {}
    for key, a in first.items():
        b = second[key]
        if math.isnan(a) and math.isnan(b):
            continue
        if math.isnan(a) != math.isnan(b) or abs(a - b) > TOLERANCE:
            differing[key] = (a, b)
    assert not differing, f"撹乱で変化した特徴量がある（リーク）: {differing}"


def test_team_rate_target_game_mutation_does_not_change_features(
    seeded_db: sqlite3.Connection,
) -> None:
    """**本命。** 対象試合を撹乱しても、クラブ視点の32列が不変であること。

    `own_{stat}_l10` は `team_game_stats` を読むため、対象試合の行が窓に
    混ざっていればここで落ちる。
    """
    game_id, as_of, club_id = _team_rate_target(seeded_db)
    before = _team_rate_features(seeded_db, game_id, as_of, club_id)

    seeded_db.execute(
        "UPDATE games SET home_score = 200, away_score = 0, attendance = 99999"
        " WHERE id = ?", (game_id,),
    )
    seeded_db.execute(
        "UPDATE team_game_stats SET pts = 200, possessions = 200,"
        " fg2m = 60, fg2a = 60, fg3m = 20, fg3a = 20, ftm = 20, fta = 20,"
        " oreb = 99, dreb = 99, ast = 99, tov = 99, stl = 99, blk = 99,"
        " pf = 6, fd = 99 WHERE game_id = ?", (game_id,),
    )
    seeded_db.execute(
        "UPDATE team_games SET result = 1, margin = 200 WHERE game_id = ?", (game_id,),
    )
    seeded_db.commit()

    _assert_same_allowing_nan(
        before, _team_rate_features(seeded_db, game_id, as_of, club_id))


def test_team_rate_future_game_mutation_does_not_change_features(
    seeded_db: sqlite3.Connection,
) -> None:
    """`as_of` 以降に終了した試合を撹乱しても不変であること。"""
    game_id, as_of, club_id = _team_rate_target(seeded_db, offset_from_end=30)
    before = _team_rate_features(seeded_db, game_id, as_of, club_id)

    boundary = as_of.isoformat(timespec="seconds").replace("+00:00", "Z")
    stats = seeded_db.execute(
        "UPDATE team_game_stats SET pts = 199, possessions = 200,"
        " fg2m = 1, fg2a = 1, fg3m = 1, fg3a = 1, ftm = 1, fta = 1,"
        " oreb = 99, dreb = 99, ast = 99, tov = 99, stl = 99, blk = 99,"
        " pf = 6, fd = 99 WHERE game_id IN"
        " (SELECT id FROM games WHERE finished_at > ?)", (boundary,),
    ).rowcount
    changed = seeded_db.execute(
        "UPDATE team_games SET result = 1, margin = 99 WHERE finished_at > ?",
        (boundary,),
    ).rowcount
    seeded_db.commit()
    assert stats > 0, "ボックススコアの撹乱対象がない。テストが空振りしている"
    assert changed > 0, "撹乱対象がない。テストが空振りしている"

    _assert_same_allowing_nan(
        before, _team_rate_features(seeded_db, game_id, as_of, club_id))


def test_team_rate_as_of_is_actually_applied(seeded_db: sqlite3.Connection) -> None:
    """**陽性確認。** `as_of` を前にずらすと値が変化すること。

    これが通らない実装は `as_of` を見ていない。撹乱テストだけでは検出できない。
    """
    game_id, as_of, club_id = _team_rate_target(seeded_db)
    now = _team_rate_features(seeded_db, game_id, as_of, club_id)
    past = _team_rate_features(
        seeded_db, game_id, as_of - timedelta(days=30), club_id)
    changed = [
        key for key in now
        if not (math.isnan(now[key]) and math.isnan(past[key]))
        and (math.isnan(now[key]) != math.isnan(past[key])
             or abs(now[key] - past[key]) > TOLERANCE)
    ]
    assert any(key.startswith(("own_", "opponent_", "pace_")) for key in changed), (
        f"as_of を30日戻しても結果依存の列が変わらない: {changed}"
    )


def test_team_rate_excludes_unfinished_games(seeded_db: sqlite3.Connection) -> None:
    """`SCHEDULED` の試合が集計に入らないこと。

    過去の試合を未実施に書き換えても、`finished_at` が NULL になるため窓から消える。
    **消えることではなく、未実施の値が混ざらないことを見る。**
    """
    game_id, as_of, club_id = _team_rate_target(seeded_db)
    before = _team_rate_features(seeded_db, game_id, as_of, club_id)

    # `as_of` より後の試合に、ありえないスタッツを入れて `SCHEDULED` にする
    boundary = as_of.isoformat(timespec="seconds").replace("+00:00", "Z")
    changed = seeded_db.execute(
        "UPDATE games SET status = 'SCHEDULED', finished_at = NULL,"
        " home_score = NULL, away_score = NULL WHERE finished_at > ?", (boundary,),
    ).rowcount
    seeded_db.commit()
    assert changed > 0, "撹乱対象がない。テストが空振りしている"

    _assert_same_allowing_nan(
        before, _team_rate_features(seeded_db, game_id, as_of, club_id))


def test_team_rate_keys_are_fixed(seeded_db: sqlite3.Connection) -> None:
    """列の集合と順序が定義と一致すること。"""
    game_id, as_of, club_id = _team_rate_target(seeded_db)
    row = _team_rate_features(seeded_db, game_id, as_of, club_id)
    assert tuple(row) == all_feature_keys()


def test_team_rate_mutation_trial_detects_an_intentional_leak(
    seeded_db: sqlite3.Connection,
) -> None:
    """**テストのテスト。** 意図的にリークさせた実装で撹乱テストが落ちること。"""
    game_id, as_of, club_id = _team_rate_target(seeded_db)

    def leaky(connection: sqlite3.Connection) -> dict[str, float]:
        base = _team_rate_features(connection, game_id, as_of, club_id)
        row = connection.execute(
            "SELECT fg3a FROM team_game_stats WHERE game_id = ? AND club_id = ?",
            (game_id, club_id),
        ).fetchone()
        return {**base, "own_fg3a_l10": base["own_fg3a_l10"] + float(row[0])}

    before = leaky(seeded_db)
    seeded_db.execute(
        "UPDATE team_game_stats SET fg3a = 99, fg3m = 1 WHERE game_id = ? AND club_id = ?",
        (game_id, club_id),
    )
    seeded_db.commit()

    with pytest.raises(AssertionError):
        _assert_same_allowing_nan(before, leaky(seeded_db))
