/**
 * レスポンス形状とエラーコード（詳細設計 3.1）。
 *
 * **エラーの詳細を返さない。** 例外オブジェクトの内容を message に入れると、
 * URL に含まれるIDやトークン断片が公開される（CLAUDE.md 絶対ルール4）。
 */
import type { Context } from 'hono';

export const CODES = {
  BAD_REQUEST: 400,
  UNAUTHORIZED: 401,
  NOT_FOUND: 404,
  ALREADY_FINAL: 409,
  RATE_LIMITED: 429,
  INTERNAL: 500,
} as const;

export type ErrorCode = keyof typeof CODES;

/** 内部エンドポイントの応答はキャッシュさせない（詳細設計 3.4）。 */
const NO_STORE = { 'Cache-Control': 'no-store' } as const;

export function ok(c: Context, data: unknown, meta: Record<string, unknown> = {}) {
  return c.json({ data, meta: { generatedAt: new Date().toISOString(), ...meta } }, 200, NO_STORE);
}

/**
 * 公開エンドポイント用。**キャッシュ方針を明示的に受け取る。**
 *
 * 既定を `no-store` にしてあるのは内部エンドポイントのためであり、公開側で
 * 指定を忘れるとキャッシュが効かず D1 に直撃する。`ok()` と分けることで、
 * 方針を書かずに公開できないようにする（詳細設計 3.5）。
 */
export function okCached(
  c: Context,
  data: unknown,
  cacheControl: string,
  meta: Record<string, unknown> = {},
) {
  return c.json({ data, meta: { generatedAt: new Date().toISOString(), ...meta } }, 200, {
    'Cache-Control': cacheControl,
  });
}

export function fail(c: Context, code: ErrorCode, message: string) {
  return c.json({ error: { code, message } }, CODES[code], NO_STORE);
}

/** Zod の失敗を 400 に変換する。どこで落ちたかだけを返す。 */
export function failValidation(c: Context, issues: readonly { path: PropertyKey[]; message: string }[]) {
  const first = issues[0];
  const where = first?.path.map(String).join('.') || '(root)';
  return fail(c, 'BAD_REQUEST', `入力が不正: ${where}: ${first?.message ?? ''}`);
}

/**
 * JSON として読めない本文を呼び出し側が 400 にできる形で返す。
 * **例外は握り、型名も出さない**（CLAUDE.md 絶対ルール4）。
 */
export type JsonResult = { ok: true; value: unknown } | { ok: false };

export async function readJson(c: Context): Promise<JsonResult> {
  try {
    return { ok: true, value: await c.req.json() };
  } catch {
    return { ok: false };
  }
}
