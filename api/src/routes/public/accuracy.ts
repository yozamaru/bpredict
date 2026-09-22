/**
 * `GET /accuracy` — 的中率（詳細設計 3.3）。
 *
 * **`accuracy_summary` を単一テーブルから読む。** 日次バッチが畳んであるため、
 * `prediction_results` の全件走査をしない（読み取りは数十行に収まる）。
 *
 * 的中率は良い時も悪い時も同じ場所に出す（要件 8.3）。ここで良い数字だけを
 * 返す分岐を作らない。
 */
import { Hono } from 'hono';

import { CACHE } from '../../lib/cache';
import { okCached } from '../../lib/http';

export type Env = { DB: D1Database };

export const accuracy = new Hono<{ Bindings: Env }>();

type SummaryRow = {
  scope: string;
  scope_key: string;
  model_version: string;
  n: number;
  accuracy: number;
  brier: number;
  actual_rate: number | null;
  baseline_accuracy: number | null;
};

/** モデル横断の集計では `model_version` に空文字が入る（詳細設計 1.6）。 */
const ACROSS_MODELS = '';

function overall(rows: SummaryRow[]) {
  const row = rows.find((r) => r.scope === 'OVERALL' && r.model_version === ACROSS_MODELS);
  if (!row) return null;
  return {
    accuracy: row.accuracy,
    brier: row.brier,
    n: row.n,
    baselineAccuracy: row.baseline_accuracy,
  };
}

function byProvisional(rows: SummaryRow[]) {
  const pick = (key: string) => {
    const row = rows.find((r) => r.scope === 'PROVISIONAL' && r.scope_key === key);
    return row ? { accuracy: row.accuracy, n: row.n } : null;
  };
  return { provisional: pick('provisional'), confirmed: pick('confirmed') };
}

accuracy.get('/accuracy', async (c) => {
  // 1クエリで全 scope を取る。数十行しかない
  const { results } = await c.env.DB.prepare(
    'SELECT scope, scope_key, model_version, n, accuracy, brier, actual_rate,'
    + ' baseline_accuracy FROM accuracy_summary ORDER BY scope, scope_key, model_version',
  ).all<SummaryRow>();

  const rows = results ?? [];
  return okCached(
    c,
    {
      overall: overall(rows),
      bySeason: rows
        .filter((r) => r.scope === 'SEASON' && r.model_version === ACROSS_MODELS)
        .map((r) => ({
          seasonId: r.scope_key,
          accuracy: r.accuracy,
          brier: r.brier,
          n: r.n,
          baselineAccuracy: r.baseline_accuracy,
        })),
      byModel: rows
        .filter((r) => r.scope === 'MODEL')
        .map((r) => ({
          modelVersion: r.scope_key,
          accuracy: r.accuracy,
          brier: r.brier,
          n: r.n,
        })),
      // **較正は「予想した確率」と「実際に勝った割合」の対比。** 母数を必ず添える
      calibration: rows
        .filter((r) => r.scope === 'BUCKET' && r.model_version === ACROSS_MODELS)
        .map((r) => ({
          bucket: r.scope_key,
          predicted: r.accuracy,
          actual: r.actual_rate,
          n: r.n,
        })),
      byProvisional: byProvisional(rows),
    },
    CACHE.settled,
  );
});
