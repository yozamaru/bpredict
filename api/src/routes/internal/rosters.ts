/**
 * 登録選手一覧の取り込み（詳細設計 1.2 / 3.4 / 4.13）。
 *
 * 出典は `/roster/?year&club` で、**試合データではない**。
 *
 * - `POST /internal/masters` では受けない（あれは**事前に列挙できるもの**に限る）
 * - `POST /internal/games` でも受けない（あちらは**試合データから導かれる**マスタ）
 *
 * `players` と `player_seasons` は**単一の `batch()`** に入れ、FK の順に
 * **`players` を先に**置く。別リクエストに分けると、途中で失敗したときに
 * 「断面はあるが人物がいない」状態が残る。
 */
import { Hono } from 'hono';

import { maxRowsPerRequest, MAX_QUERIES_PER_REQUEST, type TableName } from '../../config/batch-limits';
import { fail, failValidation, ok, readJson } from '../../lib/http';
import { upsertStatements, type Row } from '../../lib/sql';
import { rostersBody } from '../../schemas/rosters';

export type Env = { DB: D1Database };

export const rosters = new Hono<{ Bindings: Env }>();

const PLAYER_COLS = ['id', 'name', 'height_cm'] as const;

const PLAYER_SEASON_COLS = [
  'player_id', 'season_id', 'club_id', 'number', 'position', 'roster_type',
  'joined_on', 'left_on',
] as const;

function overLimit(entries: Array<[TableName, number]>): string | null {
  for (const [table, n] of entries) {
    const limit = maxRowsPerRequest(table);
    if (n > limit) return `${table} の行数が上限 ${limit} を超えている: ${n}`;
  }
  return null;
}

rosters.post('/rosters', async (c) => {
  const json = await readJson(c);
  if (!json.ok) return fail(c, 'BAD_REQUEST', 'JSON として解釈できない');
  const parsed = rostersBody.safeParse(json.value);
  if (!parsed.success) return failValidation(c, parsed.error.issues);
  const players = parsed.data.players ?? [];
  const playerSeasons = parsed.data.playerSeasons ?? [];

  const tooMany = overLimit([['players', players.length], ['player_seasons', playerSeasons.length]]);
  if (tooMany) return fail(c, 'BAD_REQUEST', tooMany);

  const playerRows: Row[] = players.map((p) => [p.id, p.name, p.heightCm ?? null]);
  const seasonRows: Row[] = playerSeasons.map((s) => [
    s.playerId, s.seasonId, s.clubId, s.number ?? null, s.position ?? null,
    s.rosterType ?? null, s.joinedOn ?? null, s.leftOn ?? null,
  ]);

  const stmts = [
    // **FK の順序。** player_seasons.player_id が players(id) を参照する
    ...upsertStatements(c.env.DB, 'players', PLAYER_COLS, playerRows, {
      conflict: ['id'],
      update: ['name', 'height_cm'],
      // **身長を NULL で上書きしない**（詳細設計 3.4）。このジョブは送らないため、
      // 将来 `/roster_detail/` から入れることになっても先に消すことはない
      preserve: ['height_cm'],
      extra: ["updated_at = datetime('now')"],
    }),
    ...upsertStatements(c.env.DB, 'player_seasons', PLAYER_SEASON_COLS, seasonRows, {
      conflict: ['player_id', 'season_id', 'club_id'],
      update: ['number', 'position', 'roster_type', 'joined_on', 'left_on'],
      // **登録区分と加入日・離脱日を NULL で上書きしない。** いずれも出典が無く
      // このジョブは送らない（要件 5.3）。入ったときに消さないよう先に保護する
      preserve: ['roster_type', 'joined_on', 'left_on'],
    }),
  ];
  if (stmts.length > MAX_QUERIES_PER_REQUEST) {
    return fail(c, 'BAD_REQUEST', `文数が上限を超えている: ${stmts.length}`);
  }
  await c.env.DB.batch(stmts);
  return ok(c, {
    applied: { players: players.length, playerSeasons: playerSeasons.length },
    statements: stmts.length,
    statementBudget: MAX_QUERIES_PER_REQUEST,
  });
});
