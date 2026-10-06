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
import { evaluateBody, logBody, statSummaryBody, summaryBody } from '../../schemas/ops';

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
  'baseline_accuracy', 'score_mae',
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
    s.actualRate ?? null, s.baselineAccuracy ?? null, s.scoreMae ?? null,
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
  // **「試合行がある」ではなく「スタッツまで入っている」を取り込み済みとする。**
  //
  // backfill は1試合につき `POST /internal/games` と `POST /internal/stats` を
  // 続けて投げる。その間で失敗すると（D1 の書き込み枠の枯渇、一時的な 5xx）、
  // 試合行だけが残る。取り込み済みの判定を試合行の有無で行うと、再開時に
  // **その試合が「済み」と見なされ、スタッツが永久に欠ける**。
  //
  // スタッツの書き込みは単一 `batch()` で原子的なので、1行でもあれば揃っている。
  // 未実施の試合（`daily_ingest` が先に入れる `SCHEDULED`）はスタッツを持たない
  // ため「未取り込み」と出るが、backfill 側が `status` を見て飛ばすので害はない。
  const rows = await c.env.DB.prepare(
    `SELECT g.id FROM games g
      WHERE g.season_id = ?
        AND EXISTS (SELECT 1 FROM team_game_stats s WHERE s.game_id = g.id)
      ORDER BY g.id`,
  )
    .bind(seasonId)
    .all<{ id: string }>();
  const gameIds = rows.results.map((r) => r.id);
  return ok(c, { seasonId, count: gameIds.length, gameIds });
});

/**
 * `GET /internal/venues` — 座標を解決する会場の一覧（詳細設計 3.4 / 4.10）。
 *
 * **スナップショットで代替しない。** 会場の集合は事前に列挙できず（詳細設計 1.2）、
 * 取り込みとともに増える。座標の解決は取り込みが終わってから1回だけ流すため、
 * そのときスナップショットが最新である保証がない（`daily_ingest` が書き出す）。
 * これは入力データの読み取りではなく運用上の読み取りであり、絶対ルール3の射程外。
 */
ops.get('/venues', async (c) => {
  // 既定は全件。`missingCoordinates=1` で座標が未解決の行だけに絞る
  const only = c.req.query('missingCoordinates');
  if (only !== undefined && only !== '1') {
    return fail(c, 'BAD_REQUEST', 'missingCoordinates は 1 のみ');
  }
  const where = only === '1' ? ' WHERE lat IS NULL OR lng IS NULL' : '';
  const rows = await c.env.DB.prepare(
    `SELECT id, name, prefecture, lat, lng FROM venues${where} ORDER BY id`,
  ).all<{
    id: string; name: string; prefecture: string | null; lat: number | null; lng: number | null;
  }>();
  return ok(c, { count: rows.results.length, venues: rows.results });
});

const PLAYER_STAT_COLS = [
  'player_id', 'scope', 'scope_key', 'club_id', 'games', 'games_started', 'minutes',
  'fg2m', 'fg2a', 'fg3m', 'fg3a', 'ftm', 'fta', 'oreb', 'dreb',
  'ast', 'tov', 'stl', 'blk', 'pf', 'fd', 'pts',
] as const;

const TEAM_STAT_COLS = [
  'club_id', 'scope', 'scope_key', 'games', 'wins', 'points_for', 'points_against',
  'stat_games', 'fg2m', 'fg2a', 'fg3m', 'fg3a', 'ftm', 'fta', 'oreb', 'dreb',
  'ast', 'tov', 'stl', 'blk', 'pf', 'fd',
] as const;

/**
 * 実績の集計（詳細設計 1.9 / 3.4 / 4.14）。
 *
 * **洗い替えない。upsert だけである。** 約4,650行は1リクエスト（160行）に収まらず、
 * 分割すると後のリクエストの DELETE が前のリクエストで入れた行を消す
 * （`/internal/ratings` が「同じ as_of_date を2リクエストに分けない」と警戒して
 * いるのと同じ形）。upsert で足りる根拠は、取り込みが試合を削除しないことである。
 *
 * **`preserve` を置かない。** この口はこの2表の唯一の書き込み経路であり、
 * 列を送らない別の経路が存在しない（Zod が全列を要求する）。
 */
ops.post('/stat-summary', async (c) => {
  const json = await readJson(c);
  if (!json.ok) return fail(c, 'BAD_REQUEST', 'JSON として解釈できない');
  const parsed = statSummaryBody.safeParse(json.value);
  if (!parsed.success) return failValidation(c, parsed.error.issues);
  const players = parsed.data.playerStats ?? [];
  const teams = parsed.data.teamStats ?? [];

  for (const [table, n] of [
    ['player_stat_summary', players.length],
    ['team_stat_summary', teams.length],
  ] as const) {
    const over = overLimit(table, n);
    if (over) return fail(c, 'BAD_REQUEST', over);
  }

  const playerRows: Row[] = players.map((s) => [
    s.playerId, s.scope, s.scopeKey, s.clubId, s.games, s.gamesStarted, s.minutes,
    s.fg2m, s.fg2a, s.fg3m, s.fg3a, s.ftm, s.fta, s.oreb, s.dreb,
    s.ast, s.tov, s.stl, s.blk, s.pf, s.fd, s.pts,
  ]);
  const teamRows: Row[] = teams.map((s) => [
    s.clubId, s.scope, s.scopeKey, s.games, s.wins, s.pointsFor, s.pointsAgainst,
    s.statGames, s.fg2m, s.fg2a, s.fg3m, s.fg3a, s.ftm, s.fta, s.oreb, s.dreb,
    s.ast, s.tov, s.stl, s.blk, s.pf, s.fd,
  ]);

  const stmts = [
    ...upsertStatements(c.env.DB, 'player_stat_summary', PLAYER_STAT_COLS, playerRows, {
      conflict: ['player_id', 'scope', 'scope_key', 'club_id'],
      update: PLAYER_STAT_COLS.filter(
        (x) => !['player_id', 'scope', 'scope_key', 'club_id'].includes(x),
      ),
      extra: ["updated_at = datetime('now')"],
    }),
    ...upsertStatements(c.env.DB, 'team_stat_summary', TEAM_STAT_COLS, teamRows, {
      conflict: ['club_id', 'scope', 'scope_key'],
      update: TEAM_STAT_COLS.filter((x) => !['club_id', 'scope', 'scope_key'].includes(x)),
      extra: ["updated_at = datetime('now')"],
    }),
  ];
  if (stmts.length > MAX_QUERIES_PER_REQUEST) {
    return fail(c, 'BAD_REQUEST', `文数が上限を超えている: ${stmts.length}`);
  }
  await c.env.DB.batch(stmts);
  return ok(c, {
    applied: { playerStats: players.length, teamStats: teams.length },
    statements: stmts.length,
  });
});
