/**
 * レスポンス形状とエラーコード（詳細設計 3.1）。
 *
 * **エラーの詳細を返さない。** 例外オブジェクトの内容を message に入れると、
 * URL に含まれるIDやトークン断片が公開される（CLAUDE.md 絶対ルール4）。
 */
import type { Context } from 'hono';

/**
 * エラーコード → HTTP ステータス（詳細設計 3.1）。
 *
 * **`ALREADY_FINAL` が 409 である**ことがこの表の要点である — 試合開始を過ぎた
 * 予測への書き込みを拒否する関門の返り値で、予測の不変性という中核価値を守る
 * （要件 3.3 / 受け入れ基準 A-03）。
 */
export const CODES = {
  BAD_REQUEST: 400,
  UNAUTHORIZED: 401,
  NOT_FOUND: 404,
  ALREADY_FINAL: 409,
  RATE_LIMITED: 429,
  INTERNAL: 500,
} as const;

/** `CODES` の鍵。**ここに無いコードは返せない**（表を唯一の出典にする）。 */
export type ErrorCode = keyof typeof CODES;

/** 内部エンドポイントの応答はキャッシュさせない（詳細設計 3.4）。 */
const NO_STORE = { 'Cache-Control': 'no-store' } as const;

/**
 * 内部エンドポイントの成功応答。**既定で `no-store` である。**
 *
 * 公開エンドポイントは `okCached` を使う — **キャッシュ方針を指定しないと
 * 公開できない形にしてある**（指定を忘れると D1 に直撃するため、忘れられる
 * 余地を消した。詳細設計 3.3）。
 */
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

/** エラー応答。**本文に内部IDや取得元URLを含めない**（要件 4.4）。 */
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

/**
 * 本文を JSON として読む。**失敗を例外にせず `{ ok: false }` で返す。**
 *
 * 呼び出し側が 400 の文言を決めるためで、**例外の本文をそのまま応答やログに
 * 流さない**という方針（絶対ルール4）とも揃う。
 */
export async function readJson(c: Context): Promise<JsonResult> {
  try {
    return { ok: true, value: await c.req.json() };
  } catch {
    return { ok: false };
  }
}
