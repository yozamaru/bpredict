/**
 * 予測の追記と、照合対象の取得。
 *
 * **書き込みの関門として必ず行うこと**（基本設計 2.4 / 詳細設計 3.4）。
 * 1. Bearer の定数時間比較（`index.ts` のミドルウェア）
 * 2. Zod による型と値域の検証
 * 3. **`tipoff_at <= now` の試合への書き込みを 409 で拒否する**
 * 4. `is_final = 1` の行を UPDATE / DELETE しない（`WHERE is_final = 0` を必ず付ける）
 * 5. 非活性化と挿入を**単一の `batch()`** にまとめる
 * 6. 日次の書き込み行数上限を超えたら 429
 */
import { Hono } from 'hono';

import { maxRowsPerRequest, MAX_QUERIES_PER_REQUEST, STATEMENTS_BUDGET } from '../../config/batch-limits';
import { fail, failValidation, ok, readJson } from '../../lib/http';
import { insertStatements, type Row } from '../../lib/sql';
import { predictionBody } from '../../schemas/predictions';

export type Env = { DB: D1Database };

/**
 * `predictions` への1日あたりの挿入行数の上限（詳細設計 3.4）。
 *
 * **子テーブルは別枠で、独立した上限を設けない。** 子まで同じカウンタで数えると
 * `player_predictions` だけで約2,200行/日に達し、正常な運用が上限に当たって止まる。
 * この上限の目的は「トークンが漏れたときの被害を1日分に抑える」ことであり、
 * 親の本数を抑えれば子も連動して抑えられる。
 */
export const DAILY_PREDICTION_ROWS = 2000;

const PRED_COLS = [
  'id', 'game_id', 'season_id', 'model_version', 'revision', 'run_id', 'predicted_at', 'as_of',
  'data_as_of', 'home_win_prob', 'pred_margin', 'pred_total', 'pred_home_score', 'pred_away_score',
  'is_provisional', 'feature_snapshot',
] as const;

const TARGET_COLS = [
  'prediction_id', 'club_id', 'is_home', 'tgt_fg2a', 'tgt_fg3a', 'tgt_fta', 'tgt_fg2_pct',
  'tgt_fg3_pct', 'tgt_ft_pct', 'tgt_oreb', 'tgt_dreb', 'tgt_ast', 'tgt_tov', 'tgt_stl',
  'tgt_blk', 'tgt_pf', 'tgt_fd',
] as const;

const PPRED_COLS = [
  'id', 'prediction_id', 'game_id', 'player_id', 'club_id', 'model_version', 'revision',
  'predicted_at', 'avail_prob', 'pred_minutes', 'pred_fg2a', 'pred_fg3a', 'pred_fta',
  'pred_fg2_pct', 'pred_fg3_pct', 'pred_ft_pct', 'pred_oreb', 'pred_dreb', 'pred_ast',
  'pred_tov', 'pred_stl', 'pred_blk', 'pred_pf', 'pred_fd', 'err_minutes', 'err_pts',
  'err_reb', 'err_ast', 'is_provisional',
] as const;

const REASON_COLS = [
  'prediction_id', 'rank', 'group_key', 'label_ja', 'value_text', 'favors', 'contribution',
  'base_value',
] as const;

const BUNDLE_COLS = ['prediction_id', 'model_type', 'target', 'model_version'] as const;

export const predictions = new Hono<{ Bindings: Env }>();

predictions.post('/', async (c) => {
  const json = await readJson(c);
  if (!json.ok) return fail(c, 'BAD_REQUEST', 'JSON として解釈できない');
  const raw = json.value;
  const parsed = predictionBody.safeParse(raw);
  if (!parsed.success) return failValidation(c, parsed.error.issues);
  const b = parsed.data;

  const players = b.playerPredictions ?? [];
  const targets = b.teamTargets ?? [];
  const reasons = b.reasons ?? [];
  const bundle = b.modelBundle ?? [];

  const limit = maxRowsPerRequest('player_predictions');
  if (players.length > limit) {
    return fail(c, 'BAD_REQUEST', `player_predictions の行数が上限 ${limit} を超えている: ${players.length}`);
  }

  // --- 関門3: tipoff 経過後の書き込みを拒否する ---------------------------------
  const game = await c.env.DB.prepare(
    `SELECT g.tipoff_at AS tipoffAt,
            (SELECT MAX(revision) FROM predictions p
              WHERE p.game_id = g.id AND p.model_version = ?) AS maxRevision
       FROM games g WHERE g.id = ?`,
  )
    .bind(b.modelVersion, b.gameId)
    .first<{ tipoffAt: string; maxRevision: number | null }>();

  if (!game) return fail(c, 'NOT_FOUND', '試合が存在しない');
  // 時刻は UTC の ISO 8601。文字列比較で順序が保たれる
  if (game.tipoffAt <= new Date().toISOString()) {
    return fail(c, 'ALREADY_FINAL', '試合開始時刻を過ぎているため書き込めない');
  }

  // --- 関門6: 日次の書き込み行数上限 -------------------------------------------
  const today = await c.env.DB.prepare(
    "SELECT COUNT(*) AS n FROM predictions WHERE date(created_at) = date('now')",
  ).first<{ n: number }>();
  if ((today?.n ?? 0) >= DAILY_PREDICTION_ROWS) {
    return fail(c, 'RATE_LIMITED', `predictions の日次上限 ${DAILY_PREDICTION_ROWS} 行に達した`);
  }

  const revision = (game.maxRevision ?? 0) + 1;
  const predictionId = crypto.randomUUID();

  const parentRow: Row = [
    predictionId, b.gameId, b.seasonId, b.modelVersion, revision, b.runId, b.predictedAt,
    b.asOf, b.dataAsOf, b.homeWinProb, b.predMargin ?? null, b.predTotal ?? null,
    b.predHomeScore ?? null, b.predAwayScore ?? null, b.isProvisional ?? 1, b.featureSnapshot,
  ];
  const targetRows: Row[] = targets.map((t) => [
    predictionId, t.clubId, t.isHome, t.tgtFg2a, t.tgtFg3a, t.tgtFta, t.tgtFg2Pct, t.tgtFg3Pct,
    t.tgtFtPct, t.tgtOreb, t.tgtDreb, t.tgtAst, t.tgtTov, t.tgtStl, t.tgtBlk, t.tgtPf, t.tgtFd,
  ]);
  const playerRows: Row[] = players.map((p) => [
    crypto.randomUUID(), predictionId, b.gameId, p.playerId, p.clubId, b.modelVersion, revision,
    b.predictedAt, p.availProb, p.predMinutes, p.predFg2a, p.predFg3a, p.predFta, p.predFg2Pct,
    p.predFg3Pct, p.predFtPct, p.predOreb, p.predDreb, p.predAst, p.predTov, p.predStl,
    p.predBlk, p.predPf, p.predFd, p.errMinutes ?? null, p.errPts ?? null, p.errReb ?? null,
    p.errAst ?? null, p.isProvisional ?? b.isProvisional ?? 1,
  ]);
  const reasonRows: Row[] = reasons.map((r) => [
    predictionId, r.rank, r.groupKey, r.labelJa, r.valueText, r.favors, r.contribution, r.baseValue,
  ]);
  const bundleRows: Row[] = bundle.map((m) => [
    predictionId, m.modelType, m.target ?? '', m.modelVersion,
  ]);

  // --- 関門5: 非活性化と挿入を単一の batch() に入れる ---------------------------
  // D1 にはリクエストを跨ぐトランザクションがない。親と子の非活性化も同じ batch() に含める。
  // **UPDATE には必ず `WHERE is_final = 0` を付ける**（関門4）。
  const stmts: D1PreparedStatement[] = [
    c.env.DB.prepare(
      'UPDATE player_predictions SET is_active = 0 WHERE game_id = ? AND is_active = 1 AND is_final = 0',
    ).bind(b.gameId),
    c.env.DB.prepare(
      'UPDATE predictions SET is_active = 0 WHERE game_id = ? AND is_active = 1 AND is_final = 0',
    ).bind(b.gameId),
    ...insertStatements(c.env.DB, 'predictions', PRED_COLS, [parentRow]),
    ...insertStatements(c.env.DB, 'prediction_team_targets', TARGET_COLS, targetRows),
    ...insertStatements(c.env.DB, 'player_predictions', PPRED_COLS, playerRows),
    ...insertStatements(c.env.DB, 'prediction_reasons', REASON_COLS, reasonRows),
    ...insertStatements(c.env.DB, 'prediction_model_bundle', BUNDLE_COLS, bundleRows),
  ];
  if (stmts.length > STATEMENTS_BUDGET) {
    return fail(c, 'BAD_REQUEST', `文数が予算 ${STATEMENTS_BUDGET} を超えている: ${stmts.length}`);
  }

  await c.env.DB.batch(stmts);

  return ok(c, {
    predictionId,
    revision,
    applied: {
      teamTargets: targets.length,
      playerPredictions: players.length,
      reasons: reasons.length,
      modelBundle: bundle.length,
    },
    statements: stmts.length,
    statementBudget: MAX_QUERIES_PER_REQUEST,
  });
});

/**
 * `GET /internal/predictions/pending` — 照合対象の確定予測。
 *
 * `games.status` が FINISHED / CANCELLED / POSTPONED で、`prediction_results` が
 * 未登録の `is_final = 1` の予測を返す（詳細設計 3.4）。
 */
predictions.get('/pending', async (c) => {
  const raw = Number(c.req.query('limit') ?? '200');
  const limit = Number.isFinite(raw) ? Math.min(Math.max(Math.trunc(raw), 1), 200) : 200;
  const rows = await c.env.DB.prepare(
    `SELECT p.id AS predictionId, p.game_id AS gameId, p.season_id AS seasonId,
            p.model_version AS modelVersion, p.home_win_prob AS homeWinProb,
            p.pred_home_score AS predHomeScore, p.pred_away_score AS predAwayScore,
            p.is_provisional AS wasProvisional
       FROM predictions p
       JOIN games g ON g.id = p.game_id
       LEFT JOIN prediction_results r ON r.prediction_id = p.id
      WHERE p.is_final = 1
        AND r.prediction_id IS NULL
        AND g.status IN ('FINISHED','CANCELLED','POSTPONED')
      ORDER BY g.game_date, p.game_id
      LIMIT ?`,
  )
    .bind(limit)
    .all();
  return ok(c, { count: rows.results.length, predictions: rows.results });
});
