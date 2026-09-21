"""テストのスキーマは db/migrations/*.sql をそのまま適用する。

スキーマを二重管理しない（`docs/design-basic.md` 8.1）。テスト用に別の DDL を書くと、
テストが本番のスキーマを反映しなくなる。
"""
from __future__ import annotations

import pathlib
import sqlite3

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"


def migration_files() -> list[pathlib.Path]:
    """適用順（ファイル名順）のマイグレーション一覧。"""
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def apply_migrations(con: sqlite3.Connection) -> None:
    for path in migration_files():
        con.executescript(path.read_text(encoding="utf-8"))


def split_statements(script: str) -> list[str]:
    """SQL をステートメントに分割する（トリガ内の `;` を壊さない）。"""
    out, buf = [], ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return out


def _leading_keyword(statement: str) -> str:
    """先頭のコメント行と空行を飛ばして、最初の実体行を返す。"""
    for line in statement.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        return stripped.upper()
    return ""


def ddl_statements(script: str) -> list[str]:
    """CREATE 文だけを返す。

    設計文書には説明用の UPDATE 例が、マイグレーションには先頭のコメントヘッダがある。
    どちらも素朴な前方一致では取りこぼすため、最初の実体行で判定する。
    """
    return [s for s in split_statements(script)
            if _leading_keyword(s).startswith("CREATE ")]


@pytest.fixture
def db() -> sqlite3.Connection:
    """マイグレーションを適用した in-memory DB。外部キーは有効にする。"""
    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA foreign_keys = ON")
    apply_migrations(con)
    try:
        yield con
    finally:
        con.close()


# --- 最小シード -----------------------------------------------------------------
# 予測系のトリガを検証するには、親となるマスタとファクトが必要。
# 実在の選手名・クラブ名は使わない（`docs/design-basic.md` 8.1）。

SEED_SEASON = "2026-27-PREMIER"
SEED_GAME = "g-0001"
SEED_MODEL = "winner-v1.0.0"
SEED_PREDICTION = "pred-0001"


def seed_minimal(con: sqlite3.Connection) -> None:
    con.execute(
        "INSERT INTO seasons (id, label, league, start_date, end_date) VALUES (?,?,?,?,?)",
        (SEED_SEASON, "2026-27", "PREMIER", "2026-09-01", "2027-05-31"),
    )
    con.executemany(
        "INSERT INTO clubs (id, slug, name) VALUES (?,?,?)",
        [("c-home", "home-club", "架空ホームクラブ"), ("c-away", "away-club", "架空アウェイクラブ")],
    )
    con.execute("INSERT INTO players (id, name) VALUES (?,?)", ("p-0001", "架空 選手"))
    con.execute(
        "INSERT INTO games (id, season_id, league, competition, game_date, tipoff_at,"
        " home_club_id, away_club_id, status) VALUES (?,?,?,?,?,?,?,?,?)",
        (SEED_GAME, SEED_SEASON, "PREMIER", "REGULAR", "2026-09-22",
         "2026-09-22T10:05:00Z", "c-home", "c-away", "SCHEDULED"),
    )
    con.execute(
        "INSERT INTO model_versions (version, model_type, algo, trained_at, train_rows,"
        " train_range, eval_window, params, feature_list)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (SEED_MODEL, "WINNER", "lightgbm", "2026-09-01T00:00:00Z", 6120,
         "2016-17..2025-26", "2024-25..2025-26", "{}", '["elo_diff"]'),
    )


def seed_team_games(con: sqlite3.Connection, competition: str = "REGULAR") -> None:
    """対象試合のチーム視点行を両チーム分入れる。

    `competition` は `games` 側と一致させる（`test_team_games_competition_matches_games`）。
    """
    con.executemany(
        "INSERT INTO team_games (game_id, club_id, opponent_id, season_id, game_date,"
        " is_home, competition) VALUES (?,?,?,?,?,?,?)",
        [
            (SEED_GAME, "c-home", "c-away", SEED_SEASON, "2026-09-22", 1, competition),
            (SEED_GAME, "c-away", "c-home", SEED_SEASON, "2026-09-22", 0, competition),
        ],
    )


def seed_prediction(
    con: sqlite3.Connection,
    prediction_id: str = SEED_PREDICTION,
    revision: int = 1,
) -> str:
    """親（predictions）と子4テーブルを1件ずつ入れる。

    再推論を模す場合は `revision` を進める（UNIQUE (game_id, model_version, revision)）。
    """
    con.execute(
        "INSERT INTO predictions (id, game_id, season_id, model_version, revision, run_id,"
        " predicted_at, as_of, data_as_of, home_win_prob, pred_margin, pred_total,"
        " pred_home_score, pred_away_score, feature_snapshot)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (prediction_id, SEED_GAME, SEED_SEASON, SEED_MODEL, revision, "run-0001",
         "2026-09-21T21:00:00Z", "2026-09-22T10:05:00Z", "2026-09-20T12:00:00Z",
         0.68, 6.0, 162.0, 84.0, 78.0, "{}"),
    )
    con.execute(
        "INSERT INTO prediction_team_targets (prediction_id, club_id, is_home,"
        " tgt_fg2a, tgt_fg3a, tgt_fta, tgt_fg2_pct, tgt_fg3_pct, tgt_ft_pct,"
        " tgt_oreb, tgt_dreb, tgt_ast, tgt_tov, tgt_stl, tgt_blk, tgt_pf, tgt_fd)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (prediction_id, "c-home", 1, 40.0, 25.0, 15.0, 0.52, 0.35, 0.80,
         10.0, 25.0, 20.0, 12.0, 7.0, 3.0, 18.0, 19.0),
    )
    con.execute(
        "INSERT INTO player_predictions (id, prediction_id, game_id, player_id, club_id,"
        " model_version, revision, predicted_at, avail_prob, pred_minutes,"
        " pred_fg2a, pred_fg3a, pred_fta, pred_fg2_pct, pred_fg3_pct, pred_ft_pct,"
        " pred_oreb, pred_dreb, pred_ast, pred_tov, pred_stl, pred_blk, pred_pf, pred_fd)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"pp-{prediction_id}", prediction_id, SEED_GAME, "p-0001", "c-home",
         SEED_MODEL, revision, "2026-09-21T21:00:00Z", 0.95, 31.2,
         7.8, 5.3, 3.9, 0.526, 0.434, 0.846,
         0.6, 2.5, 6.1, 2.2, 1.1, 0.3, 2.4, 3.1),
    )
    con.execute(
        "INSERT INTO prediction_reasons (prediction_id, rank, group_key, label_ja,"
        " value_text, favors, contribution, base_value) VALUES (?,?,?,?,?,?,?,?)",
        (prediction_id, 1, "TEAM_STRENGTH", "チーム力の差", "＋82ポイント", "HOME", 0.31, 0.40),
    )
    con.execute(
        "INSERT INTO prediction_model_bundle (prediction_id, model_type, target,"
        " model_version) VALUES (?,?,?,?)",
        (prediction_id, "WINNER", "", SEED_MODEL),
    )
    return prediction_id


PREDICTION_COLUMNS = {
    "id": "pred-x",
    "game_id": SEED_GAME,
    "season_id": SEED_SEASON,
    "model_version": SEED_MODEL,
    "revision": 1,
    "run_id": "run-x",
    "predicted_at": "2026-09-21T21:00:00Z",
    "as_of": "2026-09-22T10:05:00Z",
    "data_as_of": "2026-09-20T12:00:00Z",
    "home_win_prob": 0.5,
    "feature_snapshot": "{}",
}


def insert_prediction(con: sqlite3.Connection, **overrides) -> None:
    """列を1つだけ差し替えて predictions に INSERT する（CHECK 制約の検証用）。"""
    row = {**PREDICTION_COLUMNS, **overrides}
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    con.execute(f"INSERT INTO predictions ({cols}) VALUES ({marks})", tuple(row.values()))


def freeze(con: sqlite3.Connection, prediction_id: str = SEED_PREDICTION) -> None:
    """設計どおりの順序（子 → 親）で is_final を立てる（詳細設計 1.8 / 4.1）。"""
    con.execute(
        "UPDATE player_predictions SET is_final = 1 WHERE prediction_id = ? AND is_final = 0",
        (prediction_id,),
    )
    con.execute(
        "UPDATE predictions SET is_final = 1 WHERE id = ? AND is_final = 0",
        (prediction_id,),
    )
