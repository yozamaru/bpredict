-- モデルのバージョン管理
-- 出典: docs/design-detail.md 1章。**このファイルは適用後に編集しない。**
-- 変更は新しい番号のファイルを追加して行う（CLAUDE.md「マイグレーションは追記のみ」）。

CREATE TABLE model_versions (
  version        TEXT PRIMARY KEY,          -- 'winner-v1.0.0'
  model_type     TEXT NOT NULL
                 CHECK (model_type IN ('WINNER','MARGIN','TOTAL','TEAM_RATE',
                                       'PLAYER_AVAIL','PLAYER_MIN','PLAYER_RATE')),
  target         TEXT NOT NULL DEFAULT '',  -- *_RATE のみ: 'fg2a'|'fg2_pct'|... の14種
  league         TEXT NOT NULL DEFAULT 'PREMIER',
  win_prob_source TEXT CHECK (win_prob_source IN ('WINNER','MARGIN')),
                                             -- WINNER/MARGIN のみ。採用した勝率導出経路
  margin_sigma   REAL,                       -- MARGIN のみ。P(home)=Φ(margin/σ) の σ
  algo           TEXT NOT NULL,
  trained_at     TEXT NOT NULL,
  train_rows     INTEGER NOT NULL,
  train_range    TEXT NOT NULL,
  eval_window    TEXT NOT NULL,             -- 比較の公平性のため固定した評価対象
  params         TEXT NOT NULL,             -- JSON。seed 系を必ず含める
  feature_list   TEXT NOT NULL,             -- JSON配列
  feature_null_rates TEXT,                  -- JSON。欠損率30%超の検出用
  cv_accuracy    REAL,
  cv_brier       REAL,
  cv_logloss     REAL,
  cv_ece         REAL,                      -- 等頻度10ビン
  baseline_home_accuracy REAL,
  baseline_elo_brier     REAL,              -- Elo単体ロジスティック回帰
  artifact_text  TEXT,                      -- LightGBM の save_model() 出力
  artifact_sha256 TEXT,
  calibrator     TEXT,                      -- 較正器のパラメータ。使う場合のみ
  is_active      INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0,1)),
  notes          TEXT
);

-- 同一 (model_type, target, league) で有効モデルは常に1本
CREATE UNIQUE INDEX uq_model_active
  ON model_versions(model_type, target, league) WHERE is_active = 1;

-- 登録時のサイズガード（アプリ層でも同じ判定を行い、二重化する）
CREATE TRIGGER trg_model_artifact_size
BEFORE INSERT ON model_versions
WHEN NEW.artifact_text IS NOT NULL AND length(NEW.artifact_text) > 1572864  -- 1.5 MiB
BEGIN
  SELECT RAISE(ABORT, 'artifact_text exceeds 1.5MB limit');
END;
