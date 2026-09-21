/**
 * ファクトの取り込み — `games` / `stats` / `entries` / `ratings`。
 *
 * どれも冪等にする（CLAUDE.md 冪等性）。
 * - 試合は**公式試合IDを主キー**に upsert する（自然キーに `game_date` を含めない）
 * - エントリーは**当該試合の全行を洗い替える**（DELETE → INSERT を単一 `batch()`）
 * - Elo は差分更新せず、**対象期間を DELETE してから INSERT する**（単一 `batch()`）
 */
import { Hono } from 'hono';

import { maxRowsPerRequest, MAX_QUERIES_PER_REQUEST, type TableName } from '../../config/batch-limits';
import { fail, failValidation, ok, readJson } from '../../lib/http';
import { insertStatements, upsertStatements, type Row } from '../../lib/sql';
import { entriesBody, gamesBody, ratingsBody, statsBody } from '../../schemas/facts';

export type Env = { DB: D1Database };

export const facts = new Hono<{ Bindings: Env }>();

/** 行数上限を超えていないか。超えていれば D1 に触らず 400 を返す。 */
function overLimit(entries: Array<[TableName, number]>): string | null {
  for (const [table, n] of entries) {
    const limit = maxRowsPerRequest(table);
    if (n > limit) return `${table} の行数が上限 ${limit} を超えている: ${n}`;
  }
  return null;
}

const GAME_COLS = [
  'id', 'season_id', 'league', 'competition', 'game_date', 'tipoff_at', 'finished_at',
  'finished_at_is_estimated', 'home_club_id', 'away_club_id', 'venue_id', 'is_primary_venue',
  'series_game_no', 'status', 'rescheduled_to', 'home_score', 'away_score', 'attendance',
  'spectator_restricted', 'result_revision', 'source_url', 'fetched_at',
] as const;

const TEAM_GAME_COLS = [
  'game_id', 'club_id', 'opponent_id', 'season_id', 'game_date', 'finished_at', 'is_home',
  'competition', 'result', 'margin',
] as const;

facts.post('/games', async (c) => {
  const json = await readJson(c);
  if (!json.ok) return fail(c, 'BAD_REQUEST', 'JSON として解釈できない');
  const raw = json.value;
  const parsed = gamesBody.safeParse(raw);
  if (!parsed.success) return failValidation(c, parsed.error.issues);
  const { games, teamGames = [] } = parsed.data;

  const over = overLimit([['games', games.length], ['team_games', teamGames.length]]);
  if (over) return fail(c, 'BAD_REQUEST', over);

  const gameRows: Row[] = games.map((g) => [
    g.id, g.seasonId, g.league, g.competition, g.gameDate, g.tipoffAt, g.finishedAt ?? null,
    g.finishedAtIsEstimated ?? 0, g.homeClubId, g.awayClubId, g.venueId ?? null,
    g.isPrimaryVenue ?? 1, g.seriesGameNo ?? null, g.status, g.rescheduledTo ?? null,
    g.homeScore ?? null, g.awayScore ?? null, g.attendance ?? null,
    g.spectatorRestricted ?? null, g.resultRevision ?? 0, g.sourceUrl ?? null, g.fetchedAt ?? null,
  ]);
  const teamGameRows: Row[] = teamGames.map((t) => [
    t.gameId, t.clubId, t.opponentId, t.seasonId, t.gameDate, t.finishedAt ?? null, t.isHome,
    t.competition, t.result ?? null, t.margin ?? null,
  ]);

  const stmts = [
    // upsert キーは id（公式試合ID）。延期で game_date が変わっても別レコードにならない
    ...upsertStatements(c.env.DB, 'games', GAME_COLS, gameRows, {
      conflict: ['id'],
      update: GAME_COLS.filter((x) => x !== 'id'),
      extra: ["updated_at = datetime('now')"],
    }),
    ...upsertStatements(c.env.DB, 'team_games', TEAM_GAME_COLS, teamGameRows, {
      conflict: ['club_id', 'game_date', 'game_id'],
      update: ['opponent_id', 'season_id', 'finished_at', 'is_home', 'competition', 'result', 'margin'],
    }),
  ];
  if (stmts.length > MAX_QUERIES_PER_REQUEST) {
    return fail(c, 'BAD_REQUEST', `文数が上限を超えている: ${stmts.length}`);
  }
  await c.env.DB.batch(stmts);
  return ok(c, { applied: { games: games.length, teamGames: teamGames.length }, statements: stmts.length });
});

const TGS_COLS = [
  'game_id', 'club_id', 'game_date', 'is_home', 'pts', 'fg2m', 'fg2a', 'fg3m', 'fg3a', 'ftm',
  'fta', 'oreb', 'dreb', 'ast', 'tov', 'stl', 'blk', 'pf', 'fd', 'possessions', 'fetched_at',
] as const;

const PGS_COLS = [
  'game_id', 'player_id', 'club_id', 'game_date', 'started', 'minutes', 'fg2m', 'fg2a', 'fg3m',
  'fg3a', 'ftm', 'fta', 'oreb', 'dreb', 'ast', 'tov', 'stl', 'blk', 'pf', 'fd', 'plus_minus',
  'pts', 'fetched_at',
] as const;

facts.post('/stats', async (c) => {
  const json = await readJson(c);
  if (!json.ok) return fail(c, 'BAD_REQUEST', 'JSON として解釈できない');
  const raw = json.value;
  const parsed = statsBody.safeParse(raw);
  if (!parsed.success) return failValidation(c, parsed.error.issues);
  const { teamGameStats = [], playerGameStats = [] } = parsed.data;

  const over = overLimit([
    ['team_game_stats', teamGameStats.length],
    ['player_game_stats', playerGameStats.length],
  ]);
  if (over) return fail(c, 'BAD_REQUEST', over);

  const n = (v: number | null | undefined) => v ?? null;
  const tgsRows: Row[] = teamGameStats.map((s) => [
    s.gameId, s.clubId, s.gameDate, s.isHome, n(s.pts), n(s.fg2m), n(s.fg2a), n(s.fg3m),
    n(s.fg3a), n(s.ftm), n(s.fta), n(s.oreb), n(s.dreb), n(s.ast), n(s.tov), n(s.stl),
    n(s.blk), n(s.pf), n(s.fd), n(s.possessions), s.fetchedAt,
  ]);
  const pgsRows: Row[] = playerGameStats.map((s) => [
    s.gameId, s.playerId, s.clubId, s.gameDate, n(s.started), n(s.minutes), n(s.fg2m), n(s.fg2a),
    n(s.fg3m), n(s.fg3a), n(s.ftm), n(s.fta), n(s.oreb), n(s.dreb), n(s.ast), n(s.tov),
    n(s.stl), n(s.blk), n(s.pf), n(s.fd), n(s.plusMinus), n(s.pts), s.fetchedAt,
  ]);

  const stmts = [
    ...upsertStatements(c.env.DB, 'team_game_stats', TGS_COLS, tgsRows, {
      conflict: ['game_id', 'club_id'],
      update: TGS_COLS.filter((x) => x !== 'game_id' && x !== 'club_id'),
      extra: ["updated_at = datetime('now')"],
    }),
    ...upsertStatements(c.env.DB, 'player_game_stats', PGS_COLS, pgsRows, {
      conflict: ['game_id', 'player_id'],
      update: PGS_COLS.filter((x) => x !== 'game_id' && x !== 'player_id'),
      extra: ["updated_at = datetime('now')"],
    }),
  ];
  if (stmts.length > MAX_QUERIES_PER_REQUEST) {
    return fail(c, 'BAD_REQUEST', `文数が上限を超えている: ${stmts.length}`);
  }
  await c.env.DB.batch(stmts);
  return ok(c, {
    applied: { teamGameStats: teamGameStats.length, playerGameStats: playerGameStats.length },
    statements: stmts.length,
  });
});

const ENTRY_COLS = ['game_id', 'player_id', 'status', 'source', 'confidence', 'fetched_at'] as const;

facts.post('/entries', async (c) => {
  const json = await readJson(c);
  if (!json.ok) return fail(c, 'BAD_REQUEST', 'JSON として解釈できない');
  const raw = json.value;
  const parsed = entriesBody.safeParse(raw);
  if (!parsed.success) return failValidation(c, parsed.error.issues);
  const { gameId, entries } = parsed.data;

  const over = overLimit([['game_entries', entries.length]]);
  if (over) return fail(c, 'BAD_REQUEST', over);

  const rows: Row[] = entries.map((e) => [
    e.gameId, e.playerId, e.status, e.source, e.confidence ?? null, e.fetchedAt,
  ]);

  // **洗い替え。** DELETE と INSERT を単一 batch() に入れる
  // （D1 にはリクエストを跨ぐトランザクションがない）
  const stmts = [
    c.env.DB.prepare('DELETE FROM game_entries WHERE game_id = ?').bind(gameId),
    ...insertStatements(c.env.DB, 'game_entries', ENTRY_COLS, rows),
  ];
  if (stmts.length > MAX_QUERIES_PER_REQUEST) {
    return fail(c, 'BAD_REQUEST', `文数が上限を超えている: ${stmts.length}`);
  }
  await c.env.DB.batch(stmts);
  return ok(c, { applied: { entries: entries.length }, statements: stmts.length, replaced: true });
});

const RATING_COLS = [
  'club_id', 'as_of_date', 'season_id', 'elo', 'off_rating', 'def_rating', 'pace', 'games_played',
] as const;

facts.post('/ratings', async (c) => {
  const json = await readJson(c);
  if (!json.ok) return fail(c, 'BAD_REQUEST', 'JSON として解釈できない');
  const raw = json.value;
  const parsed = ratingsBody.safeParse(raw);
  if (!parsed.success) return failValidation(c, parsed.error.issues);
  const { fromDate, toDate, ratings } = parsed.data;

  const over = overLimit([['team_ratings', ratings.length]]);
  if (over) return fail(c, 'BAD_REQUEST', over);

  const rows: Row[] = ratings.map((r) => [
    r.clubId, r.asOfDate, r.seasonId, r.elo, r.offRating ?? null, r.defRating ?? null,
    r.pace ?? null, r.gamesPlayed,
  ]);

  // 期間の DELETE と INSERT を単一 batch() に入れる（原子性が必要な操作）
  const stmts = [
    c.env.DB.prepare('DELETE FROM team_ratings WHERE as_of_date BETWEEN ? AND ?').bind(fromDate, toDate),
    ...insertStatements(c.env.DB, 'team_ratings', RATING_COLS, rows),
  ];
  if (stmts.length > MAX_QUERIES_PER_REQUEST) {
    return fail(c, 'BAD_REQUEST', `文数が上限を超えている: ${stmts.length}`);
  }
  await c.env.DB.batch(stmts);
  return ok(c, {
    applied: { ratings: ratings.length },
    statements: stmts.length,
    window: { fromDate, toDate },
  });
});
