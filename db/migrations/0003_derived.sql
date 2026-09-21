-- 派生（レーティング）
-- 出典: docs/design-detail.md 1章。**このファイルは適用後に編集しない。**
-- 変更は新しい番号のファイルを追加して行う（CLAUDE.md「マイグレーションは追記のみ」）。

CREATE TABLE team_ratings (
  club_id      TEXT NOT NULL REFERENCES clubs(id),
  as_of_date   TEXT NOT NULL,
  season_id    TEXT NOT NULL REFERENCES seasons(id),
  elo          REAL NOT NULL,
  off_rating   REAL,
  def_rating   REAL,
  pace         REAL,
  games_played INTEGER NOT NULL,
  PRIMARY KEY (club_id, as_of_date)
);
