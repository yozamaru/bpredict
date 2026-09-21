/**
 * `POST /internal/masters` — マスタの投入（`seed_master` が使う唯一の書き込み口）。
 *
 * D1 への書き込みは Workers 経由に一本化されているため、マスタにも口が必要になる
 * （CLAUDE.md 絶対ルール3、詳細設計 3.4）。
 *
 * - **名前付きの配列で受ける。** テーブル名を引数に取る汎用エンドポイントにしない
 * - **冪等。** `INSERT ... ON CONFLICT DO UPDATE` で、何度実行しても同じ結果になる
 * - **単一の `batch()`。** D1 にはリクエストを跨ぐトランザクションがない
 * - **文数は50クエリ上限の内側に収める**（`batch-limits.ts`）
 */
import { Hono } from 'hono';

import {
  chunkRows,
  maxRowsPerRequest,
  MAX_QUERIES_PER_REQUEST,
  type TableName,
} from '../../config/batch-limits';
import { fail, failValidation, readJson } from '../../lib/http';
import { mastersSchema } from '../../schemas/masters';

export type Env = {
  DB: D1Database;
  INGEST_TOKEN?: string;
  INGEST_TOKEN_NEXT?: string;
};

const placeholders = (cols: number, rows: number): string =>
  Array.from({ length: rows }, () => `(${Array.from({ length: cols }, () => '?').join(',')})`).join(',');

export const masters = new Hono<{ Bindings: Env }>();

masters.post('/', async (c) => {
  // 例外オブジェクトをそのままレスポンスに入れない（絶対ルール4）
  const json = await readJson(c);
  if (!json.ok) return fail(c, 'BAD_REQUEST', 'JSON として解釈できない');
  const raw = json.value;

  const parsed = mastersSchema.safeParse(raw);
  if (!parsed.success) return failValidation(c, parsed.error.issues);
  const body = parsed.data;

  // 1リクエストあたりの行数上限を超えていないこと（詳細設計 3.4）
  const sizes: Array<[TableName, number]> = [
    ['seasons', body.seasons?.length ?? 0],
    ['clubs', body.clubs?.length ?? 0],
    ['club_source_ids', body.clubSourceIds?.length ?? 0],
  ];
  for (const [table, n] of sizes) {
    const limit = maxRowsPerRequest(table);
    if (n > limit) {
      return c.json(
        { error: { code: 'BAD_REQUEST', message: `${table} の行数が上限 ${limit} を超えている: ${n}` } },
        400,
      );
    }
  }

  const stmts: D1PreparedStatement[] = [];

  // FK の順序を守る。club_source_ids は clubs を参照する
  for (const rows of chunkRows('seasons', body.seasons ?? [])) {
    stmts.push(
      c.env.DB.prepare(
        `INSERT INTO seasons (id, label, league, start_date, end_date)
         VALUES ${placeholders(5, rows.length)}
         ON CONFLICT(id) DO UPDATE SET
           label = excluded.label, league = excluded.league,
           start_date = excluded.start_date, end_date = excluded.end_date`,
      ).bind(...rows.flatMap((s) => [s.id, s.label, s.league, s.startDate, s.endDate])),
    );
  }

  for (const rows of chunkRows('clubs', body.clubs ?? [])) {
    stmts.push(
      c.env.DB.prepare(
        `INSERT INTO clubs (id, slug, name)
         VALUES ${placeholders(3, rows.length)}
         ON CONFLICT(id) DO UPDATE SET
           slug = excluded.slug, name = excluded.name,
           updated_at = datetime('now')`,
      ).bind(...rows.flatMap((x) => [x.id, x.slug, x.name])),
    );
  }

  for (const rows of chunkRows('club_source_ids', body.clubSourceIds ?? [])) {
    stmts.push(
      c.env.DB.prepare(
        `INSERT INTO club_source_ids (source_id, club_id, valid_from, valid_to, note)
         VALUES ${placeholders(5, rows.length)}
         ON CONFLICT(source_id) DO UPDATE SET
           club_id = excluded.club_id, valid_from = excluded.valid_from,
           valid_to = excluded.valid_to, note = excluded.note`,
      ).bind(...rows.flatMap((k) => [k.sourceId, k.clubId, k.validFrom, k.validTo, k.note ?? null])),
    );
  }

  // 自分の文数が上限を超えないことを念のため確認する。
  // 上の行数チェックを通れば到達しないが、係数を変えたときの保険として残す。
  if (stmts.length > MAX_QUERIES_PER_REQUEST) {
    return c.json(
      { error: { code: 'BAD_REQUEST', message: `文数が上限を超えている: ${stmts.length}` } },
      400,
    );
  }

  if (stmts.length > 0) await c.env.DB.batch(stmts);

  return c.json(
    {
      data: {
        applied: {
          seasons: body.seasons?.length ?? 0,
          clubs: body.clubs?.length ?? 0,
          clubSourceIds: body.clubSourceIds?.length ?? 0,
        },
        // 50クエリ上限に対して何文使ったか。無料枠の監視に使う（A-10）
        statements: stmts.length,
        statementBudget: MAX_QUERIES_PER_REQUEST,
      },
      meta: { generatedAt: new Date().toISOString() },
    },
    200,
    { 'Cache-Control': 'no-store' },
  );
});
