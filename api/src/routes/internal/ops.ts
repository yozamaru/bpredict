/**
 * 照合・集計・ログ、および backfill の再開判定。
 *
 * `GET /internal/games/ingested` は**入力データの読み取りではない**。
 * backfill の再開判定という運用上の読み取りであり、絶対ルール3の射程外
 * （基本設計 1.2 / 詳細設計 3.4）。
 */
import { Hono } from 'hono';

import { maxRowsPerRequest, MAX_QUERIES_PER_REQUEST, type TableName } from '../../config/batch-limits';
import { fail, failValidation, ok, readJson } from '../../lib/http';
import { insertStatements, upsertStatements, type Row } from '../../lib/sql';
import { evaluateBody, logBody, summaryBody } from '../../schemas/ops';

export type Env = { DB: D1Database };

export const ops = new Hono<{ Bindings: Env }>();

function overLimit(table: TableName, n: number): string | null {
  const limit = maxRowsPerRequest(table);
  return n > limit ? `${table} の行数が上限 ${limit} を超えている: ${n}` : null;
}

const RESULT_COLS = [
  'prediction_id', 'game_id', 'season_id', 'model_version', 'home_win_prob', 'prob_bucket',
  'outcome', 'predicted_home_win', 'actual_home_win', 'is_correct', 'brier', 'score_mae',
  'was_provisional',
] as const;

ops.post('/evaluate', async (c) => {
  const json = await readJson(c);
  if (!json.ok) return fail(c, 'BAD_REQUEST', 'JSON として解釈できない');
  const raw = json.value;
  const parsed = evaluateBody.safeParse(raw);
  if (!parsed.success) return failValidation(c, parsed.error.issues);
  const { results } = parsed.data;

  const over = overLimit('prediction_results', results.length);
  if (over) return fail(c, 'BAD_REQUEST', over);

  const rows: Row[] = results.map((r) => [
    r.predictionId, r.gameId, r.seasonId, r.modelVersion, r.homeWinProb, r.probBucket, r.outcome,
    r.predictedHomeWin ?? null, r.actualHomeWin ?? null, r.isCorrect ?? null, r.brier ?? null,
    r.scoreMae ?? null, r.wasProvisional,
  ]);

  // 冪等。3回実行しても1行のまま（`test_evaluate_is_idempotent`）
  const stmts = upsertStatements(c.env.DB, 'prediction_results', RESULT_COLS, rows, {
    conflict: ['prediction_id'],
    update: RESULT_COLS.filter((x) => x !== 'prediction_id'),
    extra: ["evaluated_at = datetime('now')"],
  });
  if (stmts.length > MAX_QUERIES_PER_REQUEST) {
    return fail(c, 'BAD_REQUEST', `文数が上限を超えている: ${stmts.length}`);
  }
  await c.env.DB.batch(stmts);
  return ok(c, { applied: { results: results.length }, statements: stmts.length });
});

const SUMMARY_COLS = [
  'scope', 'scope_key', 'model_version', 'n', 'accuracy', 'brier', 'actual_rate',
  'baseline_accuracy',
] as const;

ops.post('/summary', async (c) => {
  const json = await readJson(c);
  if (!json.ok) return fail(c, 'BAD_REQUEST', 'JSON として解釈できない');
  const raw = json.value;
  const parsed = summaryBody.safeParse(raw);
  if (!parsed.success) return failValidation(c, parsed.error.issues);
  const { rows: input } = parsed.data;

  const over = overLimit('accuracy_summary', input.length);
  if (over) return fail(c, 'BAD_REQUEST', over);

  const rows: Row[] = input.map((s) => [
    s.scope, s.scopeKey, s.modelVersion ?? '', s.n, s.accuracy, s.brier,
    s.actualRate ?? null, s.baselineAccuracy ?? null,
  ]);

  // **洗い替え。** DELETE と INSERT を単一 batch() に入れる（詳細設計 1.6）
  const stmts = [
    c.env.DB.prepare('DELETE FROM accuracy_summary'),
    ...insertStatements(c.env.DB, 'accuracy_summary', SUMMARY_COLS, rows),
  ];
  if (stmts.length > MAX_QUERIES_PER_REQUEST) {
    return fail(c, 'BAD_REQUEST', `文数が上限を超えている: ${stmts.length}`);
  }
  await c.env.DB.batch(stmts);
  return ok(c, { applied: { rows: input.length }, statements: stmts.length, replaced: true });
});

const LOG_COLS = [
  'id', 'job', 'started_at', 'finished_at', 'status', 'rows_affected', 'd1_rows_read',
  'error_type', 'error_message',
] as const;

ops.post('/log', async (c) => {
  const json = await readJson(c);
  if (!json.ok) return fail(c, 'BAD_REQUEST', 'JSON として解釈できない');
  const raw = json.value;
  const parsed = logBody.safeParse(raw);
  if (!parsed.success) return failValidation(c, parsed.error.issues);
  const l = parsed.data;

  const row: Row = [
    l.id, l.job, l.startedAt, l.finishedAt ?? null, l.status, l.rowsAffected ?? null,
    l.d1RowsRead ?? null, l.errorType ?? null, l.errorMessage ?? null,
  ];
  // 同じ run の途中経過を上書きできるようにする（RUNNING → SUCCESS）
  const stmts = upsertStatements(c.env.DB, 'ingestion_logs', LOG_COLS, [row], {
    conflict: ['id'],
    update: LOG_COLS.filter((x) => x !== 'id'),
  });
  await c.env.DB.batch(stmts);
  return ok(c, { id: l.id, status: l.status });
});

/**
 * `GET /internal/games/ingested` — backfill の再開判定。
 *
 * 再開可能にしないと、55分経過時点で 429 を食らった際に翌日また全ページを取り直す
 * ことになり、相手サイトへの負荷を二重にかける（詳細設計 4.8）。
 */
ops.get('/games/ingested', async (c) => {
  const seasonId = c.req.query('seasonId');
  if (!seasonId) return fail(c, 'BAD_REQUEST', 'seasonId が必要');
  const rows = await c.env.DB.prepare(
    'SELECT id FROM games WHERE season_id = ? ORDER BY id',
  )
    .bind(seasonId)
    .all<{ id: string }>();
  const gameIds = rows.results.map((r) => r.id);
  return ok(c, { seasonId, count: gameIds.length, gameIds });
});
