-- マスタ（恒久・年度断面・名寄せ）
-- 出典: docs/design-detail.md 1章。**このファイルは適用後に編集しない。**
-- 変更は新しい番号のファイルを追加して行う（CLAUDE.md「マイグレーションは追記のみ」）。

-- 恒久的なクラブ。名称変更・リーグ移動があっても不変
CREATE TABLE clubs (
  id         TEXT PRIMARY KEY,
  slug       TEXT NOT NULL UNIQUE,
  name       TEXT NOT NULL,                    -- 現在の表示名
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 恒久的な選手。人物マスタに純化する
CREATE TABLE players (
  id         TEXT PRIMARY KEY,                 -- 公式サイトの選手ID
  name       TEXT NOT NULL,
  height_cm  INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE venues (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL,                    -- 現在の表示名
  prefecture TEXT,
  lat        REAL,
  lng        REAL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE seasons (
  id         TEXT PRIMARY KEY,                 -- '2026-27-PREMIER'
  label      TEXT NOT NULL,                    -- '2026-27'
  league     TEXT NOT NULL
             CHECK (league IN ('B1','B2','B3','PREMIER','ONE','NEXT')),
  start_date TEXT NOT NULL,
  end_date   TEXT NOT NULL
);

-- シーズンごとのクラブ断面。名称・リーグ・本拠は年度で変わる
CREATE TABLE club_seasons (
  club_id          TEXT NOT NULL REFERENCES clubs(id),
  season_id        TEXT NOT NULL REFERENCES seasons(id),
  name             TEXT NOT NULL,
  short_name       TEXT NOT NULL,
  league           TEXT NOT NULL
                   CHECK (league IN ('B1','B2','B3','PREMIER','ONE','NEXT')),
  primary_venue_id TEXT REFERENCES venues(id),
  color_primary    TEXT,
  color_secondary  TEXT,
  PRIMARY KEY (club_id, season_id)
);

CREATE INDEX idx_club_seasons_league ON club_seasons(season_id, league);

-- 公式サイトのチームID（旧B1 / 新リーグ）を club_id に解決する
CREATE TABLE club_source_ids (
  source_id  TEXT PRIMARY KEY,
  club_id    TEXT NOT NULL REFERENCES clubs(id),
  valid_from TEXT NOT NULL,
  valid_to   TEXT NOT NULL DEFAULT '9999-12-31',
  note       TEXT
);

CREATE INDEX idx_csi_club ON club_source_ids(club_id);

-- 選手の所属断面。シーズン途中の移籍にも対応する
CREATE TABLE player_seasons (
  player_id   TEXT NOT NULL REFERENCES players(id),
  season_id   TEXT NOT NULL REFERENCES seasons(id),
  club_id     TEXT NOT NULL REFERENCES clubs(id),
  number      TEXT,
  position    TEXT CHECK (position IN ('PG','SG','SF','PF','C')),
  roster_type TEXT CHECK (roster_type IN ('JP','NATURALIZED','ASIA','FOREIGN')),
  joined_on   TEXT,
  left_on     TEXT,
  PRIMARY KEY (player_id, season_id, club_id)
);

CREATE INDEX idx_pseason_club ON player_seasons(club_id, season_id);

-- 会場の改称・収容人数変更の履歴。過去試合は当時の値で表示する
CREATE TABLE venue_revisions (
  venue_id   TEXT NOT NULL REFERENCES venues(id),
  valid_from TEXT NOT NULL,
  valid_to   TEXT NOT NULL DEFAULT '9999-12-31',
  name       TEXT NOT NULL,
  capacity   INTEGER,
  PRIMARY KEY (venue_id, valid_from)
);

-- 会場名の表記ゆれを名寄せする
CREATE TABLE venue_source_keys (
  source_name TEXT PRIMARY KEY,
  venue_id    TEXT NOT NULL REFERENCES venues(id)
);
