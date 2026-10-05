import { env } from 'cloudflare:test';
import { beforeAll, beforeEach, describe, expect, it } from 'vitest';

import { applyMigrations, post, resetAll, seedGame } from './helpers';

beforeAll(async () => { await applyMigrations(); });
beforeEach(async () => { await resetAll(); });

const one = async <T>(sql: string, ...bind: unknown[]) =>
  env.DB.prepare(sql).bind(...bind).first<T>();

const count = async (sql: string, ...bind: unknown[]) =>
  (await env.DB.prepare(sql).bind(...bind).first<{ n: number }>())?.n ?? -1;

describe('登録選手一覧の取り込み', () => {
  it('players を先に入れるため player_seasons の FK が通る', async () => {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    const res = await post('/internal/rosters', {
      players: [{ id: 'p-1', name: '架空選手一' }],
      playerSeasons: [{
        playerId: 'p-1', seasonId: s.seasonId, clubId: s.homeId,
        number: '25', position: 'PF',
      }],
    });
    expect(res.status).toBe(200);
    expect(await count('SELECT COUNT(*) AS n FROM players WHERE id = ?', 'p-1')).toBe(1);
    const row = await one<{ number: string; position: string; roster_type: string | null }>(
      'SELECT number, position, roster_type FROM player_seasons WHERE player_id = ?', 'p-1');
    expect(row).toEqual({ number: '25', position: 'PF', roster_type: null });
  });

  it('同じ組を2回送っても1行のまま（冪等）', async () => {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    const body = {
      players: [{ id: 'p-1', name: '架空選手一' }],
      playerSeasons: [{ playerId: 'p-1', seasonId: s.seasonId, clubId: s.homeId, position: 'PG' }],
    };
    expect((await post('/internal/rosters', body)).status).toBe(200);
    expect((await post('/internal/rosters', body)).status).toBe(200);
    expect(await count('SELECT COUNT(*) AS n FROM player_seasons')).toBe(1);
  });

  it('**身長を NULL で上書きしない**（送る経路がまだ無い。詳細設計 3.4）', async () => {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    await env.DB.prepare('INSERT INTO players (id, name, height_cm) VALUES (?, ?, ?)')
      .bind('p-1', '架空選手一', 198).run();
    // このジョブは heightCm を送らない
    await post('/internal/rosters', {
      players: [{ id: 'p-1', name: '架空選手一（改名）' }],
      playerSeasons: [{ playerId: 'p-1', seasonId: s.seasonId, clubId: s.homeId, position: 'C' }],
    });
    const row = await one<{ name: string; height_cm: number | null }>(
      'SELECT name, height_cm FROM players WHERE id = ?', 'p-1');
    // 名前は更新され、身長は残る
    expect(row).toEqual({ name: '架空選手一（改名）', height_cm: 198 });
  });

  it('**登録区分と加入日を NULL で上書きしない**（どちらも出典が無い。要件 5.3）', async () => {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    await env.DB.prepare('INSERT INTO players (id, name) VALUES (?, ?)').bind('p-1', 'x').run();
    await env.DB.prepare(
      `INSERT INTO player_seasons (player_id, season_id, club_id, roster_type, joined_on)
       VALUES (?, ?, ?, 'FOREIGN', '2026-08-01')`,
    ).bind('p-1', s.seasonId, s.homeId).run();
    await post('/internal/rosters', {
      players: [{ id: 'p-1', name: 'x' }],
      playerSeasons: [{ playerId: 'p-1', seasonId: s.seasonId, clubId: s.homeId, position: 'SF' }],
    });
    const row = await one<{ position: string; roster_type: string; joined_on: string }>(
      'SELECT position, roster_type, joined_on FROM player_seasons WHERE player_id = ?', 'p-1');
    expect(row).toEqual({ position: 'SF', roster_type: 'FOREIGN', joined_on: '2026-08-01' });
  });

  it('ポジションは5値だけ。複数値の生文字列は 400（CHECK の前に境界で弾く）', async () => {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    for (const position of ['C/PF', 'G', 'pg', '']) {
      const res = await post('/internal/rosters', {
        players: [{ id: 'p-1', name: 'x' }],
        playerSeasons: [{ playerId: 'p-1', seasonId: s.seasonId, clubId: s.homeId, position }],
      });
      expect(res.status, position).toBe(400);
    }
  });

  it('背番号は文字列で受ける（0 が実在する）', async () => {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    const res = await post('/internal/rosters', {
      players: [{ id: 'p-1', name: 'x' }],
      playerSeasons: [{ playerId: 'p-1', seasonId: s.seasonId, clubId: s.homeId, number: '0', position: 'PG' }],
    });
    expect(res.status).toBe(200);
    const row = await one<{ number: string }>(
      'SELECT number FROM player_seasons WHERE player_id = ?', 'p-1');
    expect(row?.number).toBe('0');
  });

  it('全部空のリクエストは 400（呼び出し側の誤りである）', async () => {
    expect((await post('/internal/rosters', {})).status).toBe(400);
    expect((await post('/internal/rosters', { players: [], playerSeasons: [] })).status).toBe(400);
  });

  it('知らないキーは 400（汎用の口にしない）', async () => {
    const res = await post('/internal/rosters', {
      players: [{ id: 'p-1', name: 'x' }],
      playerSeasons: [],
      venues: [{ id: 'v-1', name: 'x' }],
    });
    expect(res.status).toBe(400);
  });

  it('Bearer なしは 401', async () => {
    const res = await post('/internal/rosters', { players: [{ id: 'p-1', name: 'x' }] }, { token: null });
    expect(res.status).toBe(401);
  });

  it('players だけ / playerSeasons だけでも通る', async () => {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    expect((await post('/internal/rosters', { players: [{ id: 'p-1', name: 'x' }] })).status).toBe(200);
    const res = await post('/internal/rosters', {
      playerSeasons: [{ playerId: 'p-1', seasonId: s.seasonId, clubId: s.homeId, position: 'PG' }],
    });
    expect(res.status).toBe(200);
  });

  it('行数の上限を超えたら D1 に触らず 400', async () => {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    // player_seasons は8列 → floor(100/8) × 40 = 480
    const many = Array.from({ length: 481 }, (_, i) => ({
      playerId: `p-${i}`, seasonId: s.seasonId, clubId: s.homeId, position: 'PG' as const,
    }));
    const res = await post('/internal/rosters', { playerSeasons: many });
    expect(res.status).toBe(400);
    expect(await count('SELECT COUNT(*) AS n FROM player_seasons')).toBe(0);
  });
});
