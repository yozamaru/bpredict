/**
 * モデルの登録と読み出し。
 *
 * バッチは入力データとして D1 を読まないが、**モデル artifact の読み出しは
 * 運用上の読み取り**であり `/internal/*` の GET で行う（基本設計 1.2 / 詳細設計 3.4）。
 */
import { Hono } from 'hono';

import { fail, failValidation, ok, readJson } from '../../lib/http';
import { ARTIFACT_MAX_BYTES, modelBody } from '../../schemas/models';

export type Env = { DB: D1Database };

const COLS = [
  'version', 'model_type', 'target', 'league', 'win_prob_source', 'margin_sigma', 'algo',
  'trained_at', 'train_rows', 'train_range', 'eval_window', 'params', 'feature_list',
  'feature_null_rates', 'cv_accuracy', 'cv_brier', 'cv_logloss', 'cv_ece',
  'baseline_home_accuracy', 'baseline_elo_brier', 'artifact_text', 'artifact_sha256',
  'calibrator', 'is_active', 'notes',
] as const;

export const models = new Hono<{ Bindings: Env }>();

models.post('/', async (c) => {
  const json = await readJson(c);
  if (!json.ok) return fail(c, 'BAD_REQUEST', 'JSON として解釈できない');
  const raw = json.value;
  const parsed = modelBody.safeParse(raw);
  if (!parsed.success) return failValidation(c, parsed.error.issues);
  const m = parsed.data;

  // **アプリ層でも 1.5MB 判定を行い、DBトリガと二重化する**（A-18）。
  // バイト数で測る（文字数ではない）。
  const bytes = m.artifactText ? new TextEncoder().encode(m.artifactText).byteLength : 0;
  if (bytes > ARTIFACT_MAX_BYTES) {
    return fail(
      c,
      'BAD_REQUEST',
      `artifact_text が上限 ${ARTIFACT_MAX_BYTES} バイトを超えている: ${bytes}`,
    );
  }

  const target = m.target ?? '';
  const league = m.league ?? 'PREMIER';
  const values = [
    m.version, m.modelType, target, league, m.winProbSource ?? null, m.marginSigma ?? null,
    m.algo, m.trainedAt, m.trainRows, m.trainRange, m.evalWindow, m.params, m.featureList,
    m.featureNullRates ?? null, m.cvAccuracy ?? null, m.cvBrier ?? null, m.cvLogloss ?? null,
    m.cvEce ?? null, m.baselineHomeAccuracy ?? null, m.baselineEloBrier ?? null,
    m.artifactText ?? null, m.artifactSha256 ?? null, m.calibrator ?? null, 0, m.notes ?? null,
  ];

  const stmts: D1PreparedStatement[] = [
    c.env.DB.prepare(
      `INSERT INTO model_versions (${COLS.join(', ')})
       VALUES (${COLS.map(() => '?').join(',')})`,
    ).bind(...values),
  ];
  if (m.activate) {
    // `uq_model_active` により「先に 0 にしてから 1 にする」順序でのみ成功する。
    // 有効モデル2本という状態が構造的に作れない（詳細設計 1.6）。
    stmts.push(
      c.env.DB.prepare(
        `UPDATE model_versions SET is_active = 0
          WHERE model_type = ? AND target = ? AND league = ? AND is_active = 1`,
      ).bind(m.modelType, target, league),
      c.env.DB.prepare('UPDATE model_versions SET is_active = 1 WHERE version = ?').bind(m.version),
    );
  }
  await c.env.DB.batch(stmts);
  return ok(c, { version: m.version, artifactBytes: bytes, activated: m.activate === true });
});

/**
 * `GET /internal/models/active` — 有効モデルの一覧（メタのみ）。
 *
 * **`artifact_text` を一覧に含めない。** 有効モデルは最大33本あり1本あたり最大1.5MB
 * であるため、本体を載せるとレスポンスが数十MBになる（詳細設計 3.4）。
 */
models.get('/active', async (c) => {
  const league = c.req.query('league') ?? 'PREMIER';
  const rows = await c.env.DB.prepare(
    `SELECT version, model_type AS modelType, target, algo, params,
            feature_list AS featureList, win_prob_source AS winProbSource,
            margin_sigma AS marginSigma, artifact_sha256 AS artifactSha256,
            length(artifact_text) AS artifactBytes, calibrator
       FROM model_versions
      WHERE is_active = 1 AND league = ?
      ORDER BY model_type, target`,
  )
    .bind(league)
    .all();
  return ok(c, { models: rows.results });
});

/**
 * `GET /internal/metrics/active` — 現行モデルの識別子と記録済み評価値。
 *
 * **この値を採用判定の比較に使わない。** 「学習当時のウィンドウで測った値」であり、
 * 新旧で評価対象が違えば比較にならない（詳細設計 4.6）。ログの突き合わせ用に限る。
 *
 * `models` とは別のルータにする。同じルータを2箇所にマウントすると
 * `/internal/metrics/active` が一覧（`/active`）に当たってしまう。
 */
export const metrics = new Hono<{ Bindings: Env }>();

metrics.get('/active', async (c) => {
  const modelType = c.req.query('modelType') ?? 'WINNER';
  const target = c.req.query('target') ?? '';
  const league = c.req.query('league') ?? 'PREMIER';
  const row = await c.env.DB.prepare(
    `SELECT version, eval_window AS evalWindow, cv_brier AS cvBrier, cv_ece AS cvEce,
            train_rows AS trainRows
       FROM model_versions
      WHERE is_active = 1 AND model_type = ? AND target = ? AND league = ?`,
  )
    .bind(modelType, target, league)
    .first();
  if (!row) return fail(c, 'NOT_FOUND', '有効モデルがない');
  return ok(c, row);
});

/** `GET /internal/models/:version/artifact` — artifact 本体を1本ずつ返す。 */
models.get('/:version/artifact', async (c) => {
  const version = c.req.param('version');
  const row = await c.env.DB.prepare(
    `SELECT version, artifact_text AS artifactText, artifact_sha256 AS artifactSha256
       FROM model_versions WHERE version = ?`,
  )
    .bind(version)
    .first();
  if (!row) return fail(c, 'NOT_FOUND', 'モデルが存在しない');
  return ok(c, row);
});
