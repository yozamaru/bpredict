import { applyD1Migrations, createExecutionContext, env } from 'cloudflare:test';

import worker from '../src/index';

/** テストで使う Bearer。実トークンは Workers Secret から読む（ここでは注入する）。 */
export const TOKEN = 'test-ingest-token';

export function withToken(extra: Record<string, unknown> = {}) {
  return { ...env, INGEST_TOKEN: TOKEN, ...extra };
}

export async function post(
  path: string,
  body: unknown,
  init: { token?: string | null; envOverride?: Record<string, unknown> } = {},
) {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  const token = init.token === undefined ? TOKEN : init.token;
  if (token !== null) headers.Authorization = `Bearer ${token}`;
  const req = new Request(`https://example.invalid${path}`, {
    method: 'POST',
    headers,
    body: typeof body === 'string' ? body : JSON.stringify(body),
  });
  const e = init.envOverride ? { ...withToken(), ...init.envOverride } : withToken();
  return worker.fetch(req, e, createExecutionContext());
}

/**
 * `db/migrations/*.sql` をそのまま適用する。
 *
 * **テストのスキーマを二重管理しない**（CLAUDE.md）。DDL は
 * `vitest.config.ts` が `readD1Migrations('../db/migrations')` で読み、
 * `TEST_MIGRATIONS` として注入する。
 */
export async function applyMigrations() {
  await applyD1Migrations(env.DB, env.TEST_MIGRATIONS);
}

/**
 * マスタの行を消す。**このプールのバージョンには isolated storage の指定がなく、
 * D1 の中身がテストを跨いで残る。** 消さないと件数の assert が前のテストの
 * 残りを数えてしまう（実際に 0 期待で 1、30 期待で 31 になった）。
 *
 * 順序は FK の逆。`club_source_ids` が `clubs` を参照する。
 */
export async function resetMasters() {
  await env.DB.batch([
    env.DB.prepare('DELETE FROM club_source_ids'),
    env.DB.prepare('DELETE FROM clubs'),
    env.DB.prepare('DELETE FROM seasons'),
  ]);
}
