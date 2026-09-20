-- 使い捨ての計測用テーブル。本番の bpredict とは別のデータベースに作ること。
DROP TABLE IF EXISTS probe;
CREATE TABLE probe (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  k  TEXT NOT NULL,
  v  INTEGER NOT NULL
);

-- /cpu 用。player_game_stats と同じ列数（24列）にしてある
DROP TABLE IF EXISTS probe_stats;
CREATE TABLE probe_stats (
  game_id TEXT, player_id TEXT, club_id TEXT, game_date TEXT,
  started INTEGER, minutes REAL,
  fg2m INTEGER, fg2a INTEGER, fg3m INTEGER, fg3a INTEGER,
  ftm INTEGER, fta INTEGER, oreb INTEGER, dreb INTEGER,
  ast INTEGER, tov INTEGER, stl INTEGER, blk INTEGER,
  pf INTEGER, fd INTEGER, plus_minus INTEGER, pts INTEGER
);
