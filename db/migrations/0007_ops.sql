-- 運用（ジョブ実行履歴）
-- 出典: docs/design-detail.md 1章。**このファイルは適用後に編集しない。**
-- 変更は新しい番号のファイルを追加して行う（CLAUDE.md「マイグレーションは追記のみ」）。

CREATE TABLE ingestion_logs (
  id            TEXT PRIMARY KEY,
  job           TEXT NOT NULL,
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  status        TEXT NOT NULL
                CHECK (status IN ('RUNNING','SUCCESS','PARTIAL','FAILED','ABORTED')),
  rows_affected INTEGER,
  d1_rows_read  INTEGER,                      -- 無料枠の監視用
  error_type    TEXT,                         -- 例外の型名のみ
  error_message TEXT                          -- 自前の短いメッセージのみ
);

CREATE INDEX idx_inglog_job ON ingestion_logs(job, started_at DESC);
