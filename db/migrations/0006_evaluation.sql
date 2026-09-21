-- 評価（照合結果・集計層）
-- 出典: docs/design-detail.md 1章。**このファイルは適用後に編集しない。**
-- 変更は新しい番号のファイルを追加して行う（CLAUDE.md「マイグレーションは追記のみ」）。

CREATE TABLE prediction_results (
  prediction_id      TEXT PRIMARY KEY REFERENCES predictions(id),
  game_id            TEXT NOT NULL REFERENCES games(id),
  season_id          TEXT NOT NULL,             -- 非正規化
  model_version      TEXT NOT NULL,
  home_win_prob      REAL NOT NULL,             -- 非正規化。calibration 用
  prob_bucket        INTEGER NOT NULL,          -- 0-9。GROUP BY 用
  outcome            TEXT NOT NULL
                     CHECK (outcome IN ('WIN','LOSS','VOID')),
  predicted_home_win INTEGER,
  actual_home_win    INTEGER,                   -- VOID のとき NULL
  is_correct         INTEGER,
  brier              REAL,
  score_mae          REAL,
  was_provisional    INTEGER NOT NULL,
  evaluated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_predres_season ON prediction_results(model_version, season_id);

CREATE INDEX idx_predres_calib  ON prediction_results(model_version, prob_bucket);

-- 日次で洗い替える集計層
CREATE TABLE accuracy_summary (
  scope         TEXT NOT NULL
                CHECK (scope IN ('OVERALL','SEASON','MODEL','BUCKET','PROVISIONAL')),
  scope_key     TEXT NOT NULL,
  model_version TEXT NOT NULL DEFAULT '',   -- モデル横断の集計では空文字。NULL にしない
  n             INTEGER NOT NULL,
  accuracy      REAL NOT NULL,
  brier         REAL NOT NULL,
  actual_rate   REAL,            -- calibration 用
  baseline_accuracy REAL,
  updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (scope, scope_key, model_version)
);
