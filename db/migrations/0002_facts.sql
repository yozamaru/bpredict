-- ファクト（試合・スタッツ・エントリー）
-- 出典: docs/design-detail.md 1章。**このファイルは適用後に編集しない。**
-- 変更は新しい番号のファイルを追加して行う（CLAUDE.md「マイグレーションは追記のみ」）。

CREATE TABLE games (
  id               TEXT PRIMARY KEY,          -- 公式サイトの試合ID
  season_id        TEXT NOT NULL REFERENCES seasons(id),
  league           TEXT NOT NULL,             -- API応答と一致させるため非正規化
  game_date        TEXT NOT NULL,             -- 変更されうる属性
  tipoff_at        TEXT NOT NULL,
  finished_at      TEXT,                      -- 試合終了時刻。リーク判定の絞り込みはこの列で行う
  finished_at_is_estimated INTEGER NOT NULL DEFAULT 0
                   CHECK (finished_at_is_estimated IN (0,1)),
                                              -- 1 = 実測が取れず tipoff_at + 2時間 を書いた
  home_club_id     TEXT NOT NULL REFERENCES clubs(id),
  away_club_id     TEXT NOT NULL REFERENCES clubs(id),
  venue_id         TEXT REFERENCES venues(id),
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
  CHECK (home_club_id <> away_club_id),
  UNIQUE (season_id, game_date, home_club_id, away_club_id)  -- 重複検出用。upsertキーではない
);

CREATE INDEX idx_games_date     ON games(game_date, status);

CREATE INDEX idx_games_finished ON games(finished_at);

CREATE INDEX idx_games_home     ON games(home_club_id, game_date);

CREATE INDEX idx_games_away     ON games(away_club_id, game_date);

-- チーム視点の試合行。OR 条件を消し、未実施試合も扱える
CREATE TABLE team_games (
  game_id     TEXT NOT NULL REFERENCES games(id),
  club_id     TEXT NOT NULL REFERENCES clubs(id),
  opponent_id TEXT NOT NULL REFERENCES clubs(id),
  season_id   TEXT NOT NULL REFERENCES seasons(id),
  game_date   TEXT NOT NULL,
  finished_at TEXT,
  is_home     INTEGER NOT NULL CHECK (is_home IN (0,1)),
  result      INTEGER CHECK (result IN (0,1)),   -- NULL = 未実施
  margin      INTEGER,
  PRIMARY KEY (club_id, game_date, game_id)
);

CREATE INDEX idx_team_games_finished ON team_games(club_id, finished_at);

CREATE TABLE team_game_stats (
  game_id     TEXT NOT NULL REFERENCES games(id),
  club_id     TEXT NOT NULL REFERENCES clubs(id),
  game_date   TEXT NOT NULL,                   -- JOIN とソートを消すための非正規化
  is_home     INTEGER NOT NULL CHECK (is_home IN (0,1)),
  pts INTEGER,
  fg2m INTEGER, fg2a INTEGER,      -- 選手側と同じ粒度で持つ（整合化の基準になる）
  fg3m INTEGER, fg3a INTEGER,
  ftm  INTEGER, fta  INTEGER,
  oreb INTEGER, dreb INTEGER,
  ast INTEGER, tov INTEGER, stl INTEGER, blk INTEGER,
  pf INTEGER, fd INTEGER,
  possessions REAL,
  fetched_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (game_id, club_id),
  CHECK (fg2m IS NULL OR fg2a IS NULL OR fg2m <= fg2a),
  CHECK (fg3m IS NULL OR fg3a IS NULL OR fg3m <= fg3a),
  CHECK (ftm  IS NULL OR fta  IS NULL OR ftm  <= fta),
  CHECK (pts IS NULL OR (pts >= 0 AND pts <= 250))
);

CREATE INDEX idx_tgs_club ON team_game_stats(club_id, game_date);

CREATE TABLE player_game_stats (
  game_id   TEXT NOT NULL REFERENCES games(id),
  player_id TEXT NOT NULL REFERENCES players(id),
  club_id   TEXT NOT NULL REFERENCES clubs(id),
  game_date TEXT NOT NULL,
  started   INTEGER CHECK (started IN (0,1)),
  minutes   REAL CHECK (minutes IS NULL OR (minutes >= 0 AND minutes <= 60)),
  -- シュート。2P と 3P を別建てで持ち、FG は導出する
  fg2m INTEGER, fg2a INTEGER,
  fg3m INTEGER, fg3a INTEGER,
  ftm  INTEGER, fta  INTEGER,
  -- リバウンド
  oreb INTEGER, dreb INTEGER,
  -- プレー
  ast INTEGER, tov INTEGER, stl INTEGER, blk INTEGER,
  -- ファウル
  pf INTEGER,                -- F  : 自分が犯したファウル
  fd INTEGER,                -- FD : 被ファウル数
  -- 実績としてのみ保持（予測しない）
  plus_minus INTEGER,
  pts INTEGER,               -- 取得値。恒等式の検証に使う
  fetched_at TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (game_id, player_id),
  CHECK (fg2m IS NULL OR fg2a IS NULL OR fg2m <= fg2a),
  CHECK (fg3m IS NULL OR fg3a IS NULL OR fg3m <= fg3a),
  CHECK (ftm  IS NULL OR fta  IS NULL OR ftm  <= fta),
  CHECK (pf IS NULL OR (pf >= 0 AND pf <= 6))
);

CREATE INDEX idx_pgs_player ON player_game_stats(player_id, game_date);

CREATE TABLE game_entries (
  game_id    TEXT NOT NULL REFERENCES games(id),
  player_id  TEXT NOT NULL REFERENCES players(id),
  status     TEXT NOT NULL CHECK (status IN ('ENTRY','OUT','UNKNOWN')),
  source     TEXT NOT NULL CHECK (source IN ('OFFICIAL','ESTIMATED')),
  confidence REAL CHECK (confidence IS NULL OR (confidence BETWEEN 0 AND 1)),
  fetched_at TEXT NOT NULL,
  PRIMARY KEY (game_id, player_id)
);
