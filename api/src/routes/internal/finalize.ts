/**
 * freeze — 試合開始時刻を過ぎた予測を確定させる。
 *
 * **`predictions` と `player_predictions` を単一 `batch()` で、子 → 親の順に UPDATE する**
 * （詳細設計 1.8 / 4.1）。`prediction_reasons` / `prediction_team_targets` /
 * `prediction_model_bundle` は `is_final` 列を持たず、親参照トリガによって同時に
 * 凍結されるため UPDATE しない。
 *
 * **順序は任意ではなく必須である。** `trg_ppred_parent_final_*` があるため、
 * 親を先に `is_final = 1` にすると子の `0 → 1` が拒否され、**freeze 自体が失敗する**。
 *
 * `AND is_final = 0` を付けるのは冪等性のため。2回目の実行では0行が一致し、
 * トリガも発火しない。
 *
 * トークンは `FINALIZE_TOKEN`（破壊的操作のため分離）。
 */
import { Hono } from 'hono';

import { STATEMENTS_BUDGET } from '../../config/batch-limits';
import { failValidation, ok, readJson } from '../../lib/http';
import { finalizeBody } from '../../schemas/predictions';

export type Env = { DB: D1Database };

/** 1リクエストで凍結する予測の上限。1件あたり2文なので予算から決まる。 */
export const MAX_FREEZE_PER_REQUEST = Math.floor(STATEMENTS_BUDGET / 2);

export type FreezeResult = { frozen: string[]; remaining: number; statements: number };

/**
 * 対象を探して凍結する。ルートと Cron Trigger の両方から呼ぶ。
 *
 * @param gameIds 指定があればその試合に限定する。無ければ `tipoff_at <= now` の全件
 */
export async function freeze(db: D1Database, gameIds?: readonly string[]): Promise<FreezeResult> {
  const now = new Date().toISOString();
  const filter = gameIds?.length ? ` AND p.game_id IN (${gameIds.map(() => '?').join(',')})` : '';
  const found = await db
    .prepare(
      `SELECT p.id AS id FROM predictions p
         JOIN games g ON g.id = p.game_id
        WHERE p.is_active = 1 AND p.is_final = 0 AND g.tipoff_at <= ?${filter}
        ORDER BY g.tipoff_at
        LIMIT ?`,
    )
    .bind(now, ...(gameIds ?? []), MAX_FREEZE_PER_REQUEST + 1)
    .all<{ id: string }>();

  const all = found.results.map((r) => r.id);
  const batchIds = all.slice(0, MAX_FREEZE_PER_REQUEST);
  const remaining = Math.max(all.length - batchIds.length, 0);
  if (batchIds.length === 0) return { frozen: [], remaining: 0, statements: 0 };

  // **この順序で固定する。** 親を先に確定させると freeze 自体が失敗する
  const stmts = batchIds.flatMap((id) => [
    db
      .prepare('UPDATE player_predictions SET is_final = 1 WHERE prediction_id = ? AND is_final = 0')
      .bind(id),
    db.prepare('UPDATE predictions SET is_final = 1 WHERE id = ? AND is_final = 0').bind(id),
  ]);
  await db.batch(stmts);
  return { frozen: batchIds, remaining, statements: stmts.length };
}

export const finalize = new Hono<{ Bindings: Env }>();

finalize.post('/', async (c) => {
  // 本文なしを許す（Cron が空で叩く）
  const json = await readJson(c);
  const parsed = finalizeBody.safeParse(json.ok ? json.value : {});
  if (!parsed.success) return failValidation(c, parsed.error.issues);

  const result = await freeze(c.env.DB, parsed.data.gameIds);
  return ok(c, {
    frozen: result.frozen.length,
    predictionIds: result.frozen,
    remaining: result.remaining,
    statements: result.statements,
    maxPerRequest: MAX_FREEZE_PER_REQUEST,
  });
});
