/**
 * 内部エンドポイントの Bearer 認証。
 *
 * - **定数時間で比較する。** 両者を SHA-256 ハッシュ化してから比較する
 *   （Workers の `timingSafeEqual` は長さ不一致で例外を投げる）
 * - **`Bearer ` プレフィックスを正規表現で厳格に検証する。**
 *   `.replace("Bearer ", "")` は `Bearer ` がない場合もヘッダ全体を返すため、
 *   スキーム検証にならない（詳細設計 7.2）
 * - トークンは2キー方式（`INGEST_TOKEN` / `INGEST_TOKEN_NEXT`）で無停止回転できる
 */
import type { Context, MiddlewareHandler } from 'hono';

const BEARER = /^Bearer\s+(\S+)$/;

async function sha256(value: string): Promise<ArrayBuffer> {
  return crypto.subtle.digest('SHA-256', new TextEncoder().encode(value));
}

export async function constantTimeEq(a: string, b: string): Promise<boolean> {
  const [ha, hb] = await Promise.all([sha256(a), sha256(b)]);
  return crypto.subtle.timingSafeEqual(ha, hb);
}

function unauthorized(c: Context) {
  return c.json(
    { error: { code: 'UNAUTHORIZED', message: '認証が必要です' } },
    401,
    { 'Cache-Control': 'no-store' },
  );
}

/** 与えられた環境変数名のいずれかに一致すれば通す。 */
export function auth(secretKeys: readonly string[]): MiddlewareHandler {
  return async (c, next) => {
    const m = BEARER.exec(c.req.header('Authorization') ?? '');
    if (!m?.[1]) return unauthorized(c);
    const presented = m[1];
    const env = c.env as Record<string, string | undefined>;
    for (const key of secretKeys) {
      const secret = env[key];
      if (secret && (await constantTimeEq(presented, secret))) return next();
    }
    return unauthorized(c);
  };
}
