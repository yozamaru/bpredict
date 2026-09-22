/**
 * 内部エンドポイントに認証が付いていることを、パスを列挙して確かめる。
 *
 * 認証は `index.ts` でパスごとに明示している（トークンが用途で分離されているため
 * `/internal/*` に一括で当てられない）。**明示である以上、付け忘れが起こりうる。**
 * ルートを足して `app.use` を足し忘れると、認証なしで書き込める口ができる。
 */
import { describe, expect, it } from 'vitest';

import indexSource from '../src/index.ts?raw';
import { get, post } from './helpers';

/**
 * 内部ルータのソースを文字列として読む。**`@types/node` を足さないため、`fs` を使わない。**
 * Vite の機能なので Workers ランタイム上のテストでも使える。
 */
const ROUTE_SOURCES: Record<string, string> = import.meta.glob(
  '../src/routes/internal/*.ts',
  { query: '?raw', import: 'default', eager: true },
);

const POSTS = [
  '/internal/masters',
  '/internal/games',
  '/internal/stats',
  '/internal/entries',
  '/internal/ratings',
  '/internal/venue-revisions',
  '/internal/predictions',
  '/internal/finalize',
  '/internal/evaluate',
  '/internal/summary',
  '/internal/log',
  '/internal/models',
];

const GETS = [
  '/internal/predictions/pending',
  '/internal/games/ingested?seasonId=x',
  '/internal/models/active',
  '/internal/models/some-version/artifact',
  '/internal/metrics/active',
];

describe('認証の網羅', () => {
  it.each(POSTS)('POST %s は Bearer なしで 401', async (path) => {
    const res = await post(path, {}, { token: null });
    expect(res.status).toBe(401);
  });

  it.each(GETS)('GET %s は Bearer なしで 401', async (path) => {
    const res = await get(path, { token: null });
    expect(res.status).toBe(401);
  });

  it.each(POSTS)('POST %s は誤ったトークンで 401', async (path) => {
    const res = await post(path, {}, { token: 'wrong-token' });
    expect(res.status).toBe(401);
  });

  /**
   * **上の列挙は手で書いている。** ルートを足して列挙を足し忘れると、この網羅は
   * 静かに穴が空く。そこで実装から実際のルートを導出し、列挙と突き合わせる。
   */
  it('実装にあるルートがすべて列挙されている', () => {
    // `app.route('/internal', facts)` から「ルータ変数名 → マウント先」を作る
    const mounts = new Map<string, string[]>();
    for (const match of indexSource.matchAll(
      /app\.route\(\s*'([^']+)'\s*,\s*(\w+)\s*\)/g,
    )) {
      const [, prefix, router] = match;
      if (!prefix || !router) continue;
      mounts.set(router, [...(mounts.get(router) ?? []), prefix]);
    }
    expect(mounts.size).toBeGreaterThan(0);

    const found = new Set<string>();
    for (const body of Object.values(ROUTE_SOURCES)) {
      for (const [, router] of body.matchAll(/export const (\w+) = new Hono/g)) {
        if (!router) continue;
        for (const prefix of mounts.get(router) ?? []) {
          for (const match of body.matchAll(
            new RegExp(`${router}\\.(post|get)\\(\\s*'([^']*)'`, 'g'),
          )) {
            const [, method, sub] = match;
            if (!method || sub === undefined) continue;
            // '/' はマウント先そのもの。':param' を含むパスは列挙側で実値に置き換える
            found.add(`${method.toUpperCase()} ${sub === '/' ? prefix : `${prefix}${sub}`}`);
          }
        }
      }
    }
    expect(found.size).toBeGreaterThan(0);

    // 列挙側のパスを正規化（クエリを落とし、実値をプレースホルダへ戻す）
    const listed = new Set([
      ...POSTS.map((p) => `POST ${p}`),
      ...GETS.map((p) => `GET ${(p.split('?')[0] ?? p).replace(/some-version/, ':version')}`),
    ]);
    const missing = [...found].filter((route) => !listed.has(route));
    expect(missing).toEqual([]);
  });

  it('認証を通ると 401 ではなくなる（401 が「未実装の404」を隠していないことの確認）', async () => {
    // すべて 404 なら上の網羅テストは意味を失う。少なくとも1つは先へ進むこと
    const res = await post('/internal/masters', { clubs: [] });
    expect(res.status).not.toBe(401);
    expect(res.status).not.toBe(404);
  });
});
