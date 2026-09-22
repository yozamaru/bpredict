"""工程1: D1 スキーマとマイグレーションの検証。

`docs/design-detail.md` 1章 の DDL が、そのまま適用できて意図した制約が効くことを確かめる。
アプリのコードを必要としない、スキーマだけで検証できる範囲を扱う。
"""
from __future__ import annotations

import re
import sqlite3

import pytest

from batch.tests.conftest import (
    SEED_GAME,
    SEED_MODEL,
    SEED_PREDICTION,
    apply_migrations,
    ddl_statements,
    freeze,
    insert_prediction,
    migration_files,
    seed_minimal,
    seed_prediction,
    seed_team_games,
)

# --- マイグレーションそのもの -----------------------------------------------------


def test_migrations_apply_cleanly():
    """全マイグレーションが空のDBに順に適用でき、2回適用すると失敗すること。

    冪等ではなく「適用済みを再適用しない」のが wrangler の前提。黙って通ると、
    ローカルと本番でスキーマが分岐しても気づけない。
    """
    con = sqlite3.connect(":memory:")
    apply_migrations(con)

    objects = con.execute(
        "SELECT type, COUNT(*) FROM sqlite_master WHERE sql IS NOT NULL GROUP BY type"
    ).fetchall()
    assert dict(objects) == {"table": 24, "index": 18, "trigger": 13}

    with pytest.raises(sqlite3.OperationalError):
        apply_migrations(con)
    con.close()


def test_migration_filenames_are_ordered_and_unique():
    """NNNN_name.sql 形式で、番号が重複せず連番であること。"""
    names = [p.name for p in migration_files()]
    assert names, "マイグレーションが1つもない"
    numbers = []
    for name in names:
        m = re.fullmatch(r"(\d{4})_[a-z0-9_]+\.sql", name)
        assert m, f"命名規則に合わない: {name}"
        numbers.append(int(m.group(1)))
    assert numbers == sorted(numbers), "ファイル名順と番号順が一致しない"
    assert len(set(numbers)) == len(numbers), f"番号が重複している: {numbers}"
    assert numbers == list(range(1, len(numbers) + 1)), f"連番でない: {numbers}"


#: テーブル定義の末尾に置かれる制約（列定義ではない）
_TABLE_CONSTRAINTS = ("CHECK", "UNIQUE", "PRIMARY KEY", "FOREIGN KEY", "CONSTRAINT")


def _split_top_level(body: str) -> list[str]:
    """括弧の深さ0のカンマで分割する。`CHECK (a IN ('x','y'))` を壊さない。"""
    parts, buf, depth = [], "", 0
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(buf.strip())
            buf = ""
            continue
        buf += char
    if buf.strip():
        parts.append(buf.strip())
    return parts


def _table_shape(sql: str) -> tuple[frozenset[str], tuple[str, ...]]:
    """CREATE TABLE を「列定義の集合」と「テーブル制約の並び」に分ける。

    **列の順序は比較しない。** `ALTER TABLE ADD COLUMN` で足した列は、SQLite が
    列定義の末尾に置き直すため、文書の DDL と位置が必ず食い違う。位置を一致させる
    ために文書側の列を末尾へ動かすと、ALTER を足すたびに文書の可読性が落ちていく。
    列順は挙動に影響しない（INSERT は必ず列名を明示し、`SELECT *` の読み出しも
    列名で引く）ため、不変条件にしない。

    **CHECK / UNIQUE などのテーブル制約は並びまで比較する。** こちらは意味を持ち、
    ALTER で位置が動くこともない。
    """
    body = sql[sql.index("(") + 1 : sql.rindex(")")]
    columns, constraints = set(), []
    for part in _split_top_level(body):
        if part.upper().startswith(_TABLE_CONSTRAINTS):
            constraints.append(part)
        else:
            columns.add(part)
    return frozenset(columns), tuple(constraints)


def test_migrations_match_design_doc():
    """マイグレーションのスキーマが詳細設計1章のDDLと一致すること。

    文書が実装の後ろを走り始めないようにするための検査。設計を変えるときは
    先に文書を直す（CLAUDE.md）。
    """
    doc = (migration_files()[0].parents[2] / "docs" / "design-detail.md").read_text(
        encoding="utf-8"
    )
    section = doc[doc.index("## 1. データベース定義") : doc.index("## 2. 特徴量定義")]
    blocks = re.findall(r"```sql\n(.*?)```", section, re.DOTALL)
    assert blocks, "詳細設計1章に sql ブロックが見つからない"

    def schema(scripts: list[str]) -> dict[tuple[str, str], object]:
        con = sqlite3.connect(":memory:")
        for script in scripts:
            for statement in ddl_statements(script):
                con.execute(statement)
        rows = con.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL"
        ).fetchall()
        con.close()

        def norm(value: str) -> str:
            return re.sub(r"\s+", " ", re.sub(r"--[^\n]*", "", value)).strip()

        out: dict[tuple[str, str], object] = {}
        for kind, name, sql in rows:
            normalized = norm(sql)
            out[(kind, name)] = (
                _table_shape(normalized) if kind == "table" else normalized
            )
        return out

    from_doc = schema(blocks)
    from_migrations = schema([p.read_text(encoding="utf-8") for p in migration_files()])
    assert set(from_doc) == set(from_migrations), (
        f"オブジェクトの集合が違う: 文書のみ={sorted(set(from_doc) - set(from_migrations))}"
        f" / マイグレーションのみ={sorted(set(from_migrations) - set(from_doc))}"
    )
    for key in sorted(from_doc):
        assert from_doc[key] == from_migrations[key], f"{key[0]} {key[1]} が一致しない"


# --- 確定予測の凍結（詳細設計 1.8） -----------------------------------------------


def test_freeze_transition_is_allowed(db):
    """is_final 0→1 の遷移自体は通ること（トリガが freeze を妨げないこと）。"""
    seed_minimal(db)
    seed_prediction(db)
    freeze(db)
    assert db.execute(
        "SELECT is_final FROM predictions WHERE id = ?", (SEED_PREDICTION,)
    ).fetchone()[0] == 1
    assert db.execute(
        "SELECT is_final FROM player_predictions WHERE prediction_id = ?",
        (SEED_PREDICTION,),
    ).fetchone()[0] == 1


def test_update_final_prediction_raises(db):
    seed_minimal(db)
    seed_prediction(db)
    freeze(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "UPDATE predictions SET home_win_prob = 0.9 WHERE id = ?", (SEED_PREDICTION,)
        )


def test_delete_final_prediction_raises(db):
    seed_minimal(db)
    seed_prediction(db)
    freeze(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM predictions WHERE id = ?", (SEED_PREDICTION,))


@pytest.mark.parametrize(
    "table, statement",
    [
        ("player_predictions", "UPDATE player_predictions SET pred_ast = 9 WHERE prediction_id = ?"),
        ("player_predictions", "DELETE FROM player_predictions WHERE prediction_id = ?"),
        ("prediction_reasons", "UPDATE prediction_reasons SET label_ja = 'x' WHERE prediction_id = ?"),
        ("prediction_reasons", "DELETE FROM prediction_reasons WHERE prediction_id = ?"),
        ("prediction_team_targets", "UPDATE prediction_team_targets SET tgt_ast = 21 WHERE prediction_id = ?"),
        ("prediction_team_targets", "DELETE FROM prediction_team_targets WHERE prediction_id = ?"),
        ("prediction_model_bundle", "UPDATE prediction_model_bundle SET model_version = 'x' WHERE prediction_id = ?"),
        ("prediction_model_bundle", "DELETE FROM prediction_model_bundle WHERE prediction_id = ?"),
    ],
)
def test_children_frozen_when_parent_final(db, table, statement):
    """親が is_final=1 のとき、子テーブルの UPDATE / DELETE が拒否されること。

    凍結に例外を設けない（CLAUDE.md 絶対ルール2）。根拠やスタッツだけ後から
    書き換えられるなら、予測の不変性という主張そのものが成立しない。
    """
    seed_minimal(db)
    seed_prediction(db)
    freeze(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(statement, (SEED_PREDICTION,))


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE player_predictions SET pred_ast = 9 WHERE prediction_id = ?",
        "DELETE FROM player_predictions WHERE prediction_id = ?",
        "UPDATE prediction_reasons SET label_ja = 'x' WHERE prediction_id = ?",
        "UPDATE prediction_team_targets SET tgt_ast = 21 WHERE prediction_id = ?",
        "UPDATE prediction_model_bundle SET model_version = 'x' WHERE prediction_id = ?",
    ],
)
def test_parent_referencing_children_frozen_by_parent_alone(db, statement):
    """子4テーブルすべてが、親を確定させただけで書き込めなくなること。

    `player_predictions` は自身の `is_final` が 0 のままでも親参照トリガで守られる。
    自テーブルの列だけに頼ると、freeze が子への UPDATE を取りこぼしたときに
    「親は確定済みなのに子は書き換えられる」状態が残る（詳細設計 1.8）。
    """
    seed_minimal(db)
    seed_prediction(db)
    db.execute("UPDATE predictions SET is_final = 1 WHERE id = ?", (SEED_PREDICTION,))
    assert db.execute(
        "SELECT is_final FROM player_predictions WHERE prediction_id = ?",
        (SEED_PREDICTION,),
    ).fetchone()[0] == 0, "子の is_final は 0 のままであること（前提の確認）"
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(statement, (SEED_PREDICTION,))


def test_player_predictions_is_guarded_by_its_own_flag(db):
    """`player_predictions` は自身の `is_final` でも守られていること（二重の守り）。

    親が未確定でも、子に `is_final = 1` が立っていれば書き換えられない。
    """
    seed_minimal(db)
    seed_prediction(db)
    db.execute(
        "UPDATE player_predictions SET is_final = 1 WHERE prediction_id = ?",
        (SEED_PREDICTION,),
    )
    assert db.execute(
        "SELECT is_final FROM predictions WHERE id = ?", (SEED_PREDICTION,)
    ).fetchone()[0] == 0, "親は未確定であること（前提の確認）"
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "UPDATE player_predictions SET pred_ast = 9 WHERE prediction_id = ?",
            (SEED_PREDICTION,),
        )


def test_freeze_fails_if_parent_frozen_first(db):
    """親を先に確定させると、子の `0 → 1` が拒否され freeze 自体が失敗すること。

    順序（子 → 親）は任意ではなく必須である（詳細設計 1.8 / 4.1）。
    """
    seed_minimal(db)
    seed_prediction(db)
    db.execute("UPDATE predictions SET is_final = 1 WHERE id = ?", (SEED_PREDICTION,))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "UPDATE player_predictions SET is_final = 1 WHERE prediction_id = ? AND is_final = 0",
            (SEED_PREDICTION,),
        )


def test_freeze_is_idempotent(db):
    """2回目の freeze が確定済みの行を UPDATE せず、例外にもならないこと。

    `AND is_final = 0` により0行が一致し、トリガも発火しない。
    """
    seed_minimal(db)
    seed_prediction(db)
    freeze(db)
    freeze(db)
    assert db.execute(
        "SELECT is_final FROM predictions WHERE id = ?", (SEED_PREDICTION,)
    ).fetchone()[0] == 1


# --- 単一性の担保（部分ユニークインデックス） --------------------------------------


def test_only_one_active_prediction_per_game(db):
    seed_minimal(db)
    seed_prediction(db, "pred-a")
    with pytest.raises(sqlite3.IntegrityError):
        seed_prediction(db, "pred-b")   # revision が違っても is_active=1 は1本だけ


def test_reinference_blocked_when_child_still_active(db):
    """親だけ非活性化して子を残すと、子の部分ユニークで弾かれること。

    `uq_pred_active` は (game_id)、`uq_ppred_active` は (game_id, player_id) に
    かかっている。設計が「親子の非活性化を同じ `batch()` に入れる」としている根拠。

    このテストでは親行の INSERT だけが先に成功して残る。**D1 の `batch()` は原子的**
    なので本番ではこの中間状態は残らないが、複数リクエストに分けると残る。
    """
    seed_minimal(db)
    seed_prediction(db, "pred-a", revision=1)
    db.execute("UPDATE predictions SET is_active = 0 WHERE id = 'pred-a'")
    with pytest.raises(sqlite3.IntegrityError):
        seed_prediction(db, "pred-b", revision=2)


def test_reinference_appends_after_deactivating_parent_and_child(db):
    """親と子の両方を非活性化すれば、新しい revision を追記できること（追記型）。"""
    seed_minimal(db)
    seed_prediction(db, "pred-a", revision=1)
    db.execute("UPDATE predictions SET is_active = 0 WHERE id = 'pred-a'")
    db.execute("UPDATE player_predictions SET is_active = 0 WHERE prediction_id = 'pred-a'")
    seed_prediction(db, "pred-b", revision=2)

    assert db.execute(
        "SELECT id, revision, is_active FROM predictions ORDER BY id"
    ).fetchall() == [("pred-a", 1, 0), ("pred-b", 2, 1)]
    assert db.execute(
        "SELECT COUNT(*) FROM player_predictions WHERE is_active = 1"
    ).fetchone()[0] == 1


def test_only_one_active_model_per_type(db):
    """(model_type, target, league) で有効モデルは常に1本（uq_model_active）。"""
    seed_minimal(db)
    db.execute("UPDATE model_versions SET is_active = 1 WHERE version = ?", (SEED_MODEL,))
    db.execute(
        "INSERT INTO model_versions (version, model_type, algo, trained_at, train_rows,"
        " train_range, eval_window, params, feature_list, is_active)"
        " VALUES ('winner-v1.1.0','WINNER','lightgbm','t',1,'r','w','{}','[]',0)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE model_versions SET is_active = 1 WHERE version = 'winner-v1.1.0'")


def test_rollback_restores_previous_model_version(db):
    """先に 0 にしてから 1 にする順序でのみ切り替えが成功すること。"""
    seed_minimal(db)
    db.execute("UPDATE model_versions SET is_active = 1 WHERE version = ?", (SEED_MODEL,))
    db.execute(
        "INSERT INTO model_versions (version, model_type, algo, trained_at, train_rows,"
        " train_range, eval_window, params, feature_list, is_active)"
        " VALUES ('winner-v1.1.0','WINNER','lightgbm','t',1,'r','w','{}','[]',0)"
    )
    db.execute("UPDATE model_versions SET is_active = 0 WHERE model_type = 'WINNER'")
    db.execute("UPDATE model_versions SET is_active = 1 WHERE version = 'winner-v1.1.0'")
    assert db.execute(
        "SELECT version FROM model_versions WHERE is_active = 1"
    ).fetchall() == [("winner-v1.1.0",)]


# --- サイズガードと CHECK 制約 ----------------------------------------------------


def test_artifact_size_guard(db):
    """1.5MB を超える artifact_text の登録が拒否されること（A-18）。"""
    def register(size: int):
        db.execute(
            "INSERT INTO model_versions (version, model_type, algo, trained_at, train_rows,"
            " train_range, eval_window, params, feature_list, artifact_text)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"winner-{size}", "WINNER", "lightgbm", "t", 1, "r", "w", "{}", "[]", "x" * size),
        )

    register(1_572_864)                       # ちょうど 1.5 MiB は通る
    with pytest.raises(sqlite3.IntegrityError):
        register(1_572_865)                   # 1 バイト超えたら拒否


@pytest.mark.parametrize(
    "column, value",
    [
        ("home_win_prob", 1.5),               # 0..1 の範囲外
        ("home_win_prob", -0.1),
        ("is_provisional", 2),
        ("is_final", 2),
        ("is_active", -1),
    ],
)
def test_predictions_check_constraints(db, column, value):
    seed_minimal(db)
    with pytest.raises(sqlite3.IntegrityError):
        insert_prediction(db, **{column: value})


def test_predictions_accepts_valid_row(db):
    """CHECK 制約の検証が「何を入れても落ちる」状態になっていないことの陽性確認。"""
    seed_minimal(db)
    insert_prediction(db)
    assert db.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 1


@pytest.mark.parametrize(
    "columns, values",
    [
        (("fg2m", "fg2a"), (5, 4)),           # 成功数 > 試投数
        (("fg3m", "fg3a"), (3, 2)),
        (("ftm", "fta"), (4, 3)),
        (("pf",), (7,)),                      # ファウルは 0-6
        (("minutes",), (61.0,)),              # 出場時間は 0-60
    ],
)
def test_player_game_stats_check_constraints(db, columns, values):
    seed_minimal(db)
    cols = ", ".join(columns)
    marks = ", ".join("?" for _ in columns)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            f"INSERT INTO player_game_stats (game_id, player_id, club_id, game_date,"
            f" fetched_at, {cols}) VALUES (?,?,?,?,?, {marks})",
            (SEED_GAME, "p-0001", "c-home", "2026-09-22", "t", *values),
        )


def test_team_targets_percentages_bounded(db):
    """チーム目標の成功率が 0..1 を外れたら拒否されること（構造的な保証の裏打ち）。"""
    seed_minimal(db)
    seed_prediction(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO prediction_team_targets (prediction_id, club_id, is_home,"
            " tgt_fg2a, tgt_fg3a, tgt_fta, tgt_fg2_pct, tgt_fg3_pct, tgt_ft_pct,"
            " tgt_oreb, tgt_dreb, tgt_ast, tgt_tov, tgt_stl, tgt_blk, tgt_pf, tgt_fd)"
            " VALUES (?, 'c-away', 0, 40,25,15, 1.2, 0.35, 0.80, 10,25,20,12,7,3,18,19)",
            (SEED_PREDICTION,),
        )


def test_accuracy_summary_rejects_duplicate_key(db):
    """モデル横断の集計でも主キーが重複を拒否すること（v1.5 の修正）。

    model_version が NULL 許容だと SQLite は重複を許し、ON CONFLICT も
    衝突を検出しない。空文字を入れる設計にしてある。
    """
    row = ("OVERALL", "all", 100, 0.68, 0.20, "t")
    sql = ("INSERT INTO accuracy_summary (scope, scope_key, n, accuracy, brier, updated_at)"
           " VALUES (?,?,?,?,?,?)")
    db.execute(sql, row)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(sql, row)


def test_accuracy_summary_scope_is_constrained(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO accuracy_summary (scope, scope_key, n, accuracy, brier, updated_at)"
            " VALUES ('BOGUS','x',1,0.5,0.3,'t')"
        )


def test_games_spectator_restricted_allows_null(db):
    """判定不能を NULL で表せること（v1.5 の修正）。2 のような値は拒否する。"""
    seed_minimal(db)
    db.execute(
        "INSERT INTO games (id, season_id, league, competition, game_date, tipoff_at,"
        " home_club_id, away_club_id, status, spectator_restricted) VALUES"
        " ('g-null','2026-27-PREMIER','PREMIER','REGULAR','2026-09-23',"
        " '2026-09-23T10:05:00Z','c-home','c-away','SCHEDULED', NULL)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO games (id, season_id, league, competition, game_date, tipoff_at,"
            " home_club_id, away_club_id, status, spectator_restricted) VALUES"
            " ('g-bad','2026-27-PREMIER','PREMIER','REGULAR','2026-09-24',"
            " '2026-09-24T10:05:00Z','c-home','c-away','SCHEDULED', 2)"
        )


def test_games_rejects_same_club_on_both_sides(db):
    seed_minimal(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO games (id, season_id, league, competition, game_date, tipoff_at,"
            " home_club_id, away_club_id, status) VALUES"
            " ('g-same','2026-27-PREMIER','PREMIER','REGULAR','2026-09-25',"
            " '2026-09-25T10:05:00Z','c-home','c-home','SCHEDULED')"
        )


@pytest.mark.parametrize("status", ["SCHEDULED", "FINISHED", "POSTPONED", "CANCELLED"])
def test_games_accepts_all_documented_statuses(db, status):
    seed_minimal(db)
    db.execute(
        "INSERT INTO games (id, season_id, league, competition, game_date, tipoff_at,"
        " home_club_id, away_club_id, status) VALUES (?,?,?,?,?,?,?,?,?)",
        (f"g-{status}", "2026-27-PREMIER", "PREMIER", "REGULAR", "2026-09-26",
         "2026-09-26T10:05:00Z", "c-home", "c-away", status),
    )


@pytest.mark.parametrize("competition", ["REGULAR", "PLAYOFF"])
def test_games_accepts_both_documented_competitions(db, competition):
    """取り込む2区分がそのまま入ること（要件 5.3）。"""
    seed_minimal(db)
    db.execute(
        "INSERT INTO games (id, season_id, league, competition, game_date, tipoff_at,"
        " home_club_id, away_club_id, status) VALUES (?,?,?,?,?,?,?,?,?)",
        (f"g-{competition}", "2026-27-PREMIER", "PREMIER", competition, "2026-09-27",
         "2026-09-27T10:05:00Z", "c-home", "c-away", "SCHEDULED"),
    )


@pytest.mark.parametrize("competition", ["ALLSTAR", "PRESEASON", "REGULAR_SEASON", "", "regular"])
def test_games_competition_rejects_values_outside_scope(db, competition):
    """取り込み対象外の区分が DB に入らないこと。

    オールスターを取り込むと Elo が壊れる（選抜チームであり `clubs` に存在しない）。
    パーサ側の絞り込みが抜けても、ここで止まる。
    """
    seed_minimal(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO games (id, season_id, league, competition, game_date, tipoff_at,"
            " home_club_id, away_club_id, status) VALUES (?,?,?,?,?,?,?,?,?)",
            ("g-out", "2026-27-PREMIER", "PREMIER", competition, "2026-09-28",
             "2026-09-28T10:05:00Z", "c-home", "c-away", "SCHEDULED"),
        )


def test_games_competition_is_not_nullable(db):
    """区分を書き忘れた行が入らないこと。"""
    seed_minimal(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO games (id, season_id, league, game_date, tipoff_at,"
            " home_club_id, away_club_id, status) VALUES (?,?,?,?,?,?,?,?)",
            ("g-nocomp", "2026-27-PREMIER", "PREMIER", "2026-09-29",
             "2026-09-29T10:05:00Z", "c-home", "c-away", "SCHEDULED"),
        )


@pytest.mark.parametrize("competition", ["REGULAR", "PLAYOFF"])
def test_team_games_carries_competition(db, competition):
    """`team_games` 側にも区分があること（JOIN を復活させないため）。"""
    seed_minimal(db)
    seed_team_games(db, competition=competition)
    rows = db.execute(
        "SELECT competition FROM team_games WHERE game_id = ?", (SEED_GAME,)
    ).fetchall()
    assert [r[0] for r in rows] == [competition, competition]


def test_team_games_competition_rejects_values_outside_scope(db):
    seed_minimal(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO team_games (game_id, club_id, opponent_id, season_id, game_date,"
            " is_home, competition) VALUES (?,?,?,?,?,?,?)",
            (SEED_GAME, "c-home", "c-away", "2026-27-PREMIER", "2026-09-22", 1, "ALLSTAR"),
        )


def test_venue_source_keys_keyed_by_official_code(db):
    """会場の名寄せが公式の会場ID（StadiumCD）で行われること。

    会場名で寄せる設計は v1.9 で廃止した。公式IDが全シーズンに存在するため
    （`verification/RESULTS.md`）、`club_source_ids` と同じ形にしてある。
    """
    cols = [r[1] for r in db.execute("PRAGMA table_info(venue_source_keys)")]
    assert cols == ["source_code", "venue_id"]
    pk = [r[1] for r in db.execute("PRAGMA table_info(venue_source_keys)") if r[5]]
    assert pk == ["source_code"]


def test_venues_accept_official_code_as_id(db):
    """StadiumCD をそのまま id に使えること（独自採番しない）。"""
    db.execute("INSERT INTO venues (id, name) VALUES ('169','架空総合体育館')")
    db.execute("INSERT INTO venue_source_keys (source_code, venue_id) VALUES ('169','169')")
    assert db.execute(
        "SELECT v.name FROM venue_source_keys k JOIN venues v ON v.id = k.venue_id"
        " WHERE k.source_code = '169'"
    ).fetchone()[0] == "架空総合体育館"


def test_venue_master_allows_missing_capacity_and_coordinates(db):
    """収容人数と緯度経度が NULL でも会場を登録できること。

    公式サイトに収容人数がなく、代替会場は NULL のまま進める（要件 5.3）。
    ここが NOT NULL だと、埋まっていない会場の試合を取り込めなくなる。
    """
    db.execute("INSERT INTO venues (id, name) VALUES ('3','架空アリーナ')")
    assert db.execute(
        "SELECT prefecture, lat, lng FROM venues WHERE id = '3'"
    ).fetchone() == (None, None, None)
    db.execute(
        "INSERT INTO venue_revisions (venue_id, valid_from, name) VALUES ('3','2016-09-01','架空アリーナ')"
    )
    assert db.execute(
        "SELECT capacity FROM venue_revisions WHERE venue_id = '3'"
    ).fetchone()[0] is None


def test_prediction_results_allows_void(db):
    """中止・延期は outcome='VOID' で、actual_home_win が NULL でも入ること（A-04）。"""
    seed_minimal(db)
    seed_prediction(db)
    db.execute(
        "INSERT INTO prediction_results (prediction_id, game_id, season_id, model_version,"
        " home_win_prob, prob_bucket, outcome, predicted_home_win, actual_home_win,"
        " is_correct, was_provisional) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (SEED_PREDICTION, SEED_GAME, "2026-27-PREMIER", SEED_MODEL,
         0.68, 6, "VOID", 1, None, None, 0),
    )
    assert db.execute("SELECT outcome FROM prediction_results").fetchone()[0] == "VOID"
