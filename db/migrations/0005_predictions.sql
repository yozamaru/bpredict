-- 予測（親子）
-- 出典: docs/design-detail.md 1章。**このファイルは適用後に編集しない。**
-- 変更は新しい番号のファイルを追加して行う（CLAUDE.md「マイグレーションは追記のみ」）。

CREATE TABLE predictions (
  id                TEXT PRIMARY KEY,
  game_id           TEXT NOT NULL REFERENCES games(id),
  season_id         TEXT NOT NULL,             -- accuracy 集計用に非正規化
  model_version     TEXT NOT NULL REFERENCES model_versions(version),
  revision          INTEGER NOT NULL,          -- 世代通番 1,2,3...
  run_id            TEXT NOT NULL,             -- ingestion_logs.id
  predicted_at      TEXT NOT NULL,
  as_of             TEXT NOT NULL,             -- 特徴量が参照してよい上限時刻
  data_as_of        TEXT NOT NULL,             -- 実行時点でDBにあった最新試合の終了時刻
  home_win_prob     REAL NOT NULL CHECK (home_win_prob BETWEEN 0 AND 1),
  pred_margin       REAL,
  pred_total        REAL,
  pred_home_score   REAL,
  pred_away_score   REAL,
  is_provisional    INTEGER NOT NULL DEFAULT 1 CHECK (is_provisional IN (0,1)),
  is_final          INTEGER NOT NULL DEFAULT 0 CHECK (is_final IN (0,1)),
  is_active         INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
  feature_snapshot  TEXT NOT NULL,             -- JSON
  created_at        TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (game_id, model_version, revision)
);

-- 1試合につき有効な予測は常に1本
CREATE UNIQUE INDEX uq_pred_active ON predictions(game_id) WHERE is_active = 1;

-- 確定済みも1本
CREATE UNIQUE INDEX uq_pred_final  ON predictions(game_id) WHERE is_final  = 1;

CREATE INDEX idx_pred_eval ON predictions(model_version, is_final, season_id);

-- 整合化の目標値。チーム側も「試投数 + 成功率」で持つ
CREATE TABLE prediction_team_targets (
  prediction_id TEXT NOT NULL REFERENCES predictions(id),
  club_id       TEXT NOT NULL REFERENCES clubs(id),
  is_home       INTEGER NOT NULL CHECK (is_home IN (0,1)),

  tgt_fg2a REAL NOT NULL CHECK (tgt_fg2a >= 0),
  tgt_fg3a REAL NOT NULL CHECK (tgt_fg3a >= 0),
  tgt_fta  REAL NOT NULL CHECK (tgt_fta  >= 0),
  tgt_fg2_pct REAL NOT NULL CHECK (tgt_fg2_pct BETWEEN 0 AND 1),
  tgt_fg3_pct REAL NOT NULL CHECK (tgt_fg3_pct BETWEEN 0 AND 1),
  tgt_ft_pct  REAL NOT NULL CHECK (tgt_ft_pct  BETWEEN 0 AND 1),

  tgt_oreb REAL NOT NULL CHECK (tgt_oreb >= 0),
  tgt_dreb REAL NOT NULL CHECK (tgt_dreb >= 0),
  tgt_ast  REAL NOT NULL CHECK (tgt_ast  >= 0),
  tgt_tov  REAL NOT NULL CHECK (tgt_tov  >= 0),
  tgt_stl  REAL NOT NULL CHECK (tgt_stl  >= 0),
  tgt_blk  REAL NOT NULL CHECK (tgt_blk  >= 0),
  tgt_pf   REAL NOT NULL CHECK (tgt_pf   >= 0),
  tgt_fd   REAL NOT NULL CHECK (tgt_fd   >= 0),

  PRIMARY KEY (prediction_id, club_id)
);

CREATE TABLE player_predictions (
  id             TEXT PRIMARY KEY,
  prediction_id  TEXT NOT NULL REFERENCES predictions(id),  -- 親の世代に紐付ける
  game_id        TEXT NOT NULL REFERENCES games(id),
  player_id      TEXT NOT NULL REFERENCES players(id),
  club_id        TEXT NOT NULL REFERENCES clubs(id),
  model_version  TEXT NOT NULL,
  revision       INTEGER NOT NULL,
  predicted_at   TEXT NOT NULL,

  avail_prob     REAL NOT NULL CHECK (avail_prob BETWEEN 0 AND 1),
  pred_minutes   REAL NOT NULL CHECK (pred_minutes >= 0),  -- 整合化後の期待出場時間

  -- 整合化後の試投数（カウント。期待値なので REAL）
  pred_fg2a REAL NOT NULL CHECK (pred_fg2a >= 0),
  pred_fg3a REAL NOT NULL CHECK (pred_fg3a >= 0),
  pred_fta  REAL NOT NULL CHECK (pred_fta  >= 0),

  -- 整合化後の成功率（0〜1）。成功数はこれと試投数の積で導出する
  pred_fg2_pct REAL NOT NULL CHECK (pred_fg2_pct BETWEEN 0 AND 1),
  pred_fg3_pct REAL NOT NULL CHECK (pred_fg3_pct BETWEEN 0 AND 1),
  pred_ft_pct  REAL NOT NULL CHECK (pred_ft_pct  BETWEEN 0 AND 1),

  -- その他のカウント
  pred_oreb REAL NOT NULL CHECK (pred_oreb >= 0),
  pred_dreb REAL NOT NULL CHECK (pred_dreb >= 0),
  pred_ast  REAL NOT NULL CHECK (pred_ast  >= 0),
  pred_tov  REAL NOT NULL CHECK (pred_tov  >= 0),
  pred_stl  REAL NOT NULL CHECK (pred_stl  >= 0),
  pred_blk  REAL NOT NULL CHECK (pred_blk  >= 0),
  pred_pf   REAL NOT NULL CHECK (pred_pf   >= 0),
  pred_fd   REAL NOT NULL CHECK (pred_fd   >= 0),

  -- 誤差の目安（当該選手の直近N試合の絶対誤差の中央値）。主要4項目のみ
  err_minutes REAL, err_pts REAL, err_reb REAL, err_ast REAL,

  is_provisional INTEGER NOT NULL DEFAULT 1,
  is_final       INTEGER NOT NULL DEFAULT 0,
  is_active      INTEGER NOT NULL DEFAULT 1,
  UNIQUE (game_id, player_id, model_version, revision)
);

CREATE UNIQUE INDEX uq_ppred_active ON player_predictions(game_id, player_id) WHERE is_active = 1;

CREATE TABLE prediction_reasons (
  prediction_id TEXT NOT NULL REFERENCES predictions(id),
  rank          INTEGER NOT NULL,
  group_key     TEXT NOT NULL,     -- 'TEAM_STRENGTH'|'SCHEDULE'|'PLAYER'|'VENUE'
  label_ja      TEXT NOT NULL,
  value_text    TEXT NOT NULL,
  favors        TEXT NOT NULL CHECK (favors IN ('HOME','AWAY')),
  contribution  REAL NOT NULL,     -- グループ集約したSHAP値（ログオッズ空間）
  base_value    REAL NOT NULL,     -- explainer の期待値。事後検証用
  PRIMARY KEY (prediction_id, rank)
);

-- 推論に使ったモデル一式の記録。予測1本は複数モデルの合成である
CREATE TABLE prediction_model_bundle (
  prediction_id TEXT NOT NULL REFERENCES predictions(id),
  model_type    TEXT NOT NULL,
  target        TEXT NOT NULL DEFAULT '',
  model_version TEXT NOT NULL REFERENCES model_versions(version),
  PRIMARY KEY (prediction_id, model_type, target)
);
