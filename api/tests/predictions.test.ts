import { env } from 'cloudflare:test';
import { beforeAll, beforeEach, describe, expect, it } from 'vitest';

import {
  applyMigrations, FINALIZE_TOKEN, get, post, predictionPayload, resetAll, seedGame, TOKEN,
  type Seed,
} from './helpers';

const FUTURE = '2099-01-01T10:05:00Z';
const PAST = '2000-01-01T10:05:00Z';

const player = (s: Seed) => ({
  playerId: s.playerId, clubId: s.homeId, availProb: 0.95, predMinutes: 31.2,
  predFg2a: 7.8, predFg3a: 5.3, predFta: 3.9,
  predFg2Pct: 0.526, predFg3Pct: 0.434, predFtPct: 0.846,
  predOreb: 0.6, predDreb: 2.5, predAst: 6.1, predTov: 2.2,
  predStl: 1.1, predBlk: 0.3, predPf: 2.4, predFd: 3.1,
  errMinutes: 5.8, errPts: 4.8, errReb: 2.1, errAst: 1.5,
});
const target = (clubId: string, isHome: 0 | 1) => ({
  clubId, isHome,
  tgtFg2a: 50, tgtFg3a: 25, tgtFta: 18,
  tgtFg2Pct: 0.52, tgtFg3Pct: 0.35, tgtFtPct: 0.78,
  tgtOreb: 10, tgtDreb: 26, tgtAst: 20, tgtTov: 12, tgtStl: 6, tgtBlk: 3, tgtPf: 18, tgtFd: 18,
});
const reason = {
  rank: 1, groupKey: 'TEAM_STRENGTH', labelJa: 'チーム力の差',
  valueText: '＋82ポイント', favors: 'HOME', contribution: 0.4, baseValue: 0.1,
};

/** 凍結済みの行は消せないため、テストごとに一意なIDを使う（helpers.seedGame）。 */
beforeAll(async () => { await applyMigrations(); });
beforeEach(async () => { await resetAll(); });

const count = async (sql: string, ...bind: unknown[]) =>
  (await env.DB.prepare(sql).bind(...bind).first<{ n: number }>())?.n ?? -1;

describe('tipoff ガード（受け入れ基準 A-03）', () => {
  it('試合開始時刻を過ぎていたら 409 で、1行も書き込まれない', async () => {
    const s = await seedGame({ tipoffAt: PAST });
    const res = await post('/internal/predictions', predictionPayload(s));
    expect(res.status).toBe(409);
    expect(await res.json()).toMatchObject({ error: { code: 'ALREADY_FINAL' } });
    expect(await count('SELECT COUNT(*) AS n FROM predictions WHERE game_id = ?', s.gameId)).toBe(0);
  });

  it('開始前なら通る', async () => {
    const s = await seedGame({ tipoffAt: FUTURE });
    const res = await post('/internal/predictions', predictionPayload(s));
    expect(res.status).toBe(200);
  });

  it('存在しない試合は 404', async () => {
    const s = await seedGame({ tipoffAt: FUTURE });
    const res = await post('/internal/predictions', predictionPayload({ ...s, gameId: 'missing' }));
    expect(res.status).toBe(404);
  });
});

describe('世代管理（詳細設計 1.5）', () => {
  it('再推論は上書きせず追記し、親子ともに旧行が is_active = 0 になる', async () => {
    const s = await seedGame({ tipoffAt: FUTURE });
    const body = predictionPayload(s, { playerPredictions: [player(s)] });
    const a = await post('/internal/predictions', body);
    const b = await post('/internal/predictions', body);
    expect([a.status, b.status]).toEqual([200, 200]);
    expect((await a.json<{ data: { revision: number } }>()).data.revision).toBe(1);
    expect((await b.json<{ data: { revision: number } }>()).data.revision).toBe(2);

    const parent = await env.DB
      .prepare('SELECT revision, is_active FROM predictions WHERE game_id = ? ORDER BY revision')
      .bind(s.gameId).all<{ revision: number; is_active: number }>();
    expect(parent.results).toEqual([
      { revision: 1, is_active: 0 },
      { revision: 2, is_active: 1 },
    ]);
    const child = await env.DB
      .prepare('SELECT revision, is_active FROM player_predictions WHERE game_id = ? ORDER BY revision')
      .bind(s.gameId).all<{ revision: number; is_active: number }>();
    expect(child.results).toEqual([
      { revision: 1, is_active: 0 },
      { revision: 2, is_active: 1 },
    ]);
  });

  it('親子がまとめて単一 batch() で入る', async () => {
    const s = await seedGame({ tipoffAt: FUTURE });
    const res = await post('/internal/predictions', predictionPayload(s, {
      teamTargets: [target(s.homeId, 1), target(s.awayId, 0)],
      playerPredictions: [player(s)],
      reasons: [reason],
      modelBundle: [{ modelType: 'WINNER', modelVersion: s.modelVersion }],
    }));
    expect(res.status).toBe(200);
    const body = await res.json<{ data: { applied: Record<string, number>; statements: number } }>();
    expect(body.data.applied).toEqual({
      teamTargets: 2, playerPredictions: 1, reasons: 1, modelBundle: 1,
    });
    // 50クエリ上限に対して余裕があること（受け入れ基準 A-10）
    expect(body.data.statements).toBeLessThanOrEqual(40);
  });

  it('teamTargets が1件だけなら 400（ホーム・アウェイの両方か0件）', async () => {
    const s = await seedGame({ tipoffAt: FUTURE });
    const res = await post('/internal/predictions',
      predictionPayload(s, { teamTargets: [target(s.homeId, 1)] }));
    expect(res.status).toBe(400);
  });

  it('player_predictions の行数が上限を超えたら 400', async () => {
    const s = await seedGame({ tipoffAt: FUTURE });
    const many = Array.from({ length: 121 }, () => player(s));   // 31列 → 上限 120
    const res = await post('/internal/predictions', predictionPayload(s, { playerPredictions: many }));
    expect(res.status).toBe(400);
  });
});

describe('freeze（詳細設計 1.8 / 4.1）', () => {
  async function makeFinalizable(): Promise<Seed> {
    const s = await seedGame({ tipoffAt: FUTURE });
    await post('/internal/predictions', predictionPayload(s, {
      teamTargets: [target(s.homeId, 1), target(s.awayId, 0)],
      playerPredictions: [player(s)],
      reasons: [reason],
      modelBundle: [{ modelType: 'WINNER', modelVersion: s.modelVersion }],
    }));
    // 開始時刻を過去にずらし、freeze の対象にする
    await env.DB.prepare('UPDATE games SET tipoff_at = ? WHERE id = ?').bind(PAST, s.gameId).run();
    return s;
  }

  it('子 → 親の順で凍結され、親子ともに is_final = 1 になる', async () => {
    const s = await makeFinalizable();
    const res = await post('/internal/finalize', { gameIds: [s.gameId] }, { token: FINALIZE_TOKEN });
    expect(res.status).toBe(200);
    const body = await res.json<{ data: { frozen: number; statements: number } }>();
    expect(body.data.frozen).toBe(1);
    expect(body.data.statements).toBe(2);       // 子1文 + 親1文

    expect(await count('SELECT COUNT(*) AS n FROM predictions WHERE game_id = ? AND is_final = 1', s.gameId)).toBe(1);
    expect(await count('SELECT COUNT(*) AS n FROM player_predictions WHERE game_id = ? AND is_final = 1', s.gameId)).toBe(1);
  });

  it('冪等（2回流しても対象が増えず、トリガも発火しない）', async () => {
    const s = await makeFinalizable();
    await post('/internal/finalize', { gameIds: [s.gameId] }, { token: FINALIZE_TOKEN });
    const res = await post('/internal/finalize', { gameIds: [s.gameId] }, { token: FINALIZE_TOKEN });
    expect(res.status).toBe(200);
    expect((await res.json<{ data: { frozen: number } }>()).data.frozen).toBe(0);
  });

  it('凍結後は子テーブルも書き換えられない（親参照トリガ）', async () => {
    const s = await makeFinalizable();
    await post('/internal/finalize', { gameIds: [s.gameId] }, { token: FINALIZE_TOKEN });
    const id = (await env.DB.prepare('SELECT id FROM predictions WHERE game_id = ?')
      .bind(s.gameId).first<{ id: string }>())?.id;
    for (const sql of [
      'UPDATE prediction_reasons SET label_ja = ? WHERE prediction_id = ?',
      'UPDATE prediction_team_targets SET tgt_ast = ? WHERE prediction_id = ?',
      'UPDATE prediction_model_bundle SET model_version = ? WHERE prediction_id = ?',
      'UPDATE player_predictions SET pred_ast = ? WHERE prediction_id = ?',
      'UPDATE predictions SET home_win_prob = ? WHERE id = ?',
    ]) {
      await expect(env.DB.prepare(sql).bind('9', id).run()).rejects.toThrow();
    }
  });

  it('開始前の試合は凍結されない', async () => {
    const s = await seedGame({ tipoffAt: FUTURE });
    await post('/internal/predictions', predictionPayload(s));
    const res = await post('/internal/finalize', { gameIds: [s.gameId] }, { token: FINALIZE_TOKEN });
    expect((await res.json<{ data: { frozen: number } }>()).data.frozen).toBe(0);
  });

  it('INGEST_TOKEN では通らない（トークンを用途で分離する）', async () => {
    const res = await post('/internal/finalize', {}, { token: TOKEN });
    expect(res.status).toBe(401);
  });
});

describe('照合対象の取得', () => {
  it('確定済みで未評価の予測だけを返す', async () => {
    const s = await seedGame({ tipoffAt: FUTURE });
    await post('/internal/predictions', predictionPayload(s));
    await env.DB.prepare("UPDATE games SET tipoff_at = ?, status = 'FINISHED' WHERE id = ?")
      .bind(PAST, s.gameId).run();

    // freeze 前は対象にならない（的中率は is_final = 1 の行のみを使う）
    let res = await get('/internal/predictions/pending');
    let ids = (await res.json<{ data: { predictions: { gameId: string }[] } }>()).data.predictions;
    expect(ids.some((p) => p.gameId === s.gameId)).toBe(false);

    await post('/internal/finalize', { gameIds: [s.gameId] }, { token: FINALIZE_TOKEN });
    res = await get('/internal/predictions/pending');
    ids = (await res.json<{ data: { predictions: { gameId: string }[] } }>()).data.predictions;
    expect(ids.some((p) => p.gameId === s.gameId)).toBe(true);
  });

  it('Bearer なしは 401', async () => {
    const res = await get('/internal/predictions/pending', { token: null });
    expect(res.status).toBe(401);
  });
});
