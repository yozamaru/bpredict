-- games から UNIQUE (season_id, game_date, home_club_id, away_club_id) を外す
-- 出典: docs/design-detail.md 1.3。**このファイルは適用後に編集しない。**
-- 変更は新しい番号のファイルを追加して行う（CLAUDE.md「マイグレーションは追記のみ」）。
--
-- なぜ外すのか。
--   **実データがこの仮定を満たさない。** チャンピオンシップでは、同じ日・同じカードで
--   第2戦の直後に決着戦が行われる。2016-17 の実例:
--
--     1329  2017-05-20  727 vs 706  通常の試合
--     1330  2017-05-20  727 vs 706  26-18    ← 同じ自然キー
--     1332  2017-05-21  703 vs 728  通常の試合
--     1333  2017-05-21  703 vs 728  14-12    ← 同じ自然キー
--
--   制約違反は SQLite の例外になり、Worker が 500 を返して取り込みが止まる。
--   実際に 2016-17 の再取り込みが `500 / games` で失敗した。
--
--   重複の検出は制約ではなく取り込み側で行う。`id`（公式試合ID）が主キーである以上、
--   同じ試合が二重に入ることはない。「別IDで同じカードが入る」ことは正常な事象である。
--
-- SQLite は UNIQUE 制約を単体で削除できない（暗黙のインデックスは DROP INDEX できない）。
-- テーブルを作り直す。**DROP TABLE を含むため、実行前に運営者の確認を取った。**
--
-- FK を一時的に外す。子テーブル4つ（team_games / team_game_stats / player_game_stats /
-- game_entries）と predictions 系が games を参照しており、そのままでは DROP できない。
-- legacy_alter_table を ON にするのは、RENAME で他テーブルの FK 参照を書き換えさせない
-- ため（子は "games" を参照しており、RENAME 後も同じ名前に戻る）。
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;

CREATE TABLE games_rebuild (
  id               TEXT PRIMARY KEY,          -- 公式サイトの試合ID
  season_id        TEXT NOT NULL REFERENCES seasons(id),
  league           TEXT NOT NULL,             -- API応答と一致させるため非正規化
  competition      TEXT NOT NULL              -- REGULAR = リーグ戦 / PLAYOFF = チャンピオンシップ
                   CHECK (competition IN ('REGULAR','PLAYOFF')),
  game_date        TEXT NOT NULL,             -- 変更されうる属性
  tipoff_at        TEXT NOT NULL,
  finished_at      TEXT,                      -- 試合終了時刻。リーク判定の絞り込みはこの列で行う
  finished_at_is_estimated INTEGER NOT NULL DEFAULT 0
                   CHECK (finished_at_is_estimated IN (0,1)),
                                              -- 1 = 実測が取れず tipoff_at + 2時間 を書いた
  home_club_id     TEXT NOT NULL REFERENCES clubs(id),
  away_club_id     TEXT NOT NULL REFERENCES clubs(id),
  venue_id         TEXT REFERENCES venues(id),
  venue_name_at_game TEXT,                    -- その試合時点の会場名（StadiumNameJ）。
                                              -- venue_revisions.name の唯一の入力（1.2）
  is_primary_venue INTEGER NOT NULL DEFAULT 1 CHECK (is_primary_venue IN (0,1)),
  series_game_no   INTEGER,
  status           TEXT NOT NULL
                   CHECK (status IN ('SCHEDULED','FINISHED','POSTPONED','CANCELLED')),
  rescheduled_to   TEXT REFERENCES games(id), -- 延期先。旧行は POSTPONED で残す
  home_score       INTEGER,
  away_score       INTEGER,
  attendance       INTEGER,
  spectator_restricted INTEGER              -- NULL = 判定不能（attendance か capacity が欠損）
                   CHECK (spectator_restricted IS NULL
                          OR spectator_restricted IN (0,1)),
  result_revision  INTEGER NOT NULL DEFAULT 0, -- スコア訂正のたびに +1
  source_url       TEXT,
  fetched_at       TEXT,
  created_at       TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at       TEXT NOT NULL DEFAULT (datetime('now')),
  CHECK (home_club_id <> away_club_id)
);

-- 列を明示する。`SELECT *` は列順に依存し、0009 で足した列の位置が処理系で変わりうる
INSERT INTO games_rebuild (
  id, season_id, league, competition, game_date, tipoff_at, finished_at,
  finished_at_is_estimated, home_club_id, away_club_id, venue_id, venue_name_at_game,
  is_primary_venue, series_game_no, status, rescheduled_to, home_score, away_score,
  attendance, spectator_restricted, result_revision, source_url, fetched_at,
  created_at, updated_at
)
SELECT
  id, season_id, league, competition, game_date, tipoff_at, finished_at,
  finished_at_is_estimated, home_club_id, away_club_id, venue_id, venue_name_at_game,
  is_primary_venue, series_game_no, status, rescheduled_to, home_score, away_score,
  attendance, spectator_restricted, result_revision, source_url, fetched_at,
  created_at, updated_at
FROM games;

DROP TABLE games;
ALTER TABLE games_rebuild RENAME TO games;

CREATE INDEX idx_games_date     ON games(game_date, status);
CREATE INDEX idx_games_finished ON games(finished_at);
CREATE INDEX idx_games_home     ON games(home_club_id, game_date);
CREATE INDEX idx_games_away     ON games(away_club_id, game_date);

PRAGMA legacy_alter_table = OFF;
PRAGMA foreign_keys = ON;
