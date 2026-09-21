/**
 * 内部エンドポイントに認証が付いていることを、パスを列挙して確かめる。
 *
 * 認証は `index.ts` でパスごとに明示している（トークンが用途で分離されているため
 * `/internal/*` に一括で当てられない）。**明示である以上、付け忘れが起こりうる。**
 * ルートを足して `app.use` を足し忘れると、認証なしで書き込める口ができる。
 */
import { describe, expect, it } from 'vitest';

import { get, post } from './helpers';

const POSTS = [
  '/internal/masters',
  '/internal/games',
  '/internal/stats',
  '/internal/entries',
  '/internal/ratings',
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

  it('認証を通ると 401 ではなくなる（401 が「未実装の404」を隠していないことの確認）', async () => {
    // すべて 404 なら上の網羅テストは意味を失う。少なくとも1つは先へ進むこと
    const res = await post('/internal/masters', { clubs: [] });
    expect(res.status).not.toBe(401);
    expect(res.status).not.toBe(404);
  });
});
