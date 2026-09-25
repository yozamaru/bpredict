/**
 * `GET /teams` と `GET /teams/:slug`（詳細設計 3.3）。
 *
 * **表示名は `club_seasons`（年度断面）を使う。** `clubs.name` は現在の表示名であり、
 * 過去シーズンの表示に使うと遡って変わる（詳細設計 1.2）。`slug` は `clubs` が持つ
 * 恒久の識別子で、改称でも変わらない（詳細設計 1.1）。
 */
import { Hono } from 'hono';

import { CACHE } from '../../lib/cache';
import { fail, okCached } from '../../lib/http';
import { latestSeasonId, parseLimit, parseSlug } from '../../lib/params';

export type Env = { DB: D1Database };

export const teams = new Hono<{ Bindings: Env }>();

teams.get('/teams', async (c) => {
  const seasonId = await latestSeasonId(c.env.DB);
  if (seasonId === null) return okCached(c, { seasonId: null, teams: [] }, CACHE.teams);

  const rows = await c.env.DB.prepare(
    `SELECT c.id, c.slug, cs.name, cs.short_name
       FROM club_seasons cs
       JOIN clubs c ON c.id = cs.club_id
      WHERE cs.season_id = ?
      ORDER BY c.slug`,
  ).bind(seasonId).all<{ id: string; slug: string; name: string; short_name: string }>();

  return okCached(c, {
    seasonId,
    teams: rows.results.map((row) => ({
      clubId: row.id, slug: row.slug, name: row.name, shortName: row.short_name,
    })),
  }, CACHE.teams);
});

type HistoryRow = {
  game_id: string; game_date: string; is_home: number;
  result: number | null; margin: number | null;
  opponent_id: string; opponent_slug: string;
  opponent_name: string | null; opponent_short: string | null;
  home_win_prob: number | null; is_correct: number | null;
};

teams.get('/teams/:slug', async (c) => {
  const slug = parseSlug(c.req.param('slug'));
  if (slug === null) return fail(c, 'BAD_REQUEST', 'slug の形式が不正');
  const limit = parseLimit(c.req.query('limit'));

  const club = await c.env.DB.prepare(
    'SELECT id, slug FROM clubs WHERE slug = ? LIMIT 1',
  ).bind(slug).first<{ id: string; slug: string }>();
  if (!club) return fail(c, 'NOT_FOUND', 'クラブが見つからない');

  const seasonId = await latestSeasonId(c.env.DB);
  if (seasonId === null) return fail(c, 'NOT_FOUND', 'シーズンが登録されていない');

  const season = await c.env.DB.prepare(
    'SELECT name, short_name FROM club_seasons WHERE club_id = ? AND season_id = ? LIMIT 1',
  ).bind(club.id, seasonId).first<{ name: string; short_name: string }>();

  // 当季の成績。**`team_games` だけを読む**（`games` への JOIN を消すために
  // 作った表であり、勝率・得失点差はここから取る。詳細設計 1.3）
  const record = await c.env.DB.prepare(
    `SELECT COUNT(*) AS played,
            SUM(CASE WHEN result = 1 THEN 1 ELSE 0 END) AS wins,
            AVG(margin) AS avg_margin
       FROM team_games
      WHERE club_id = ? AND season_id = ? AND result IS NOT NULL`,
  ).bind(club.id, seasonId).first<{ played: number; wins: number; avg_margin: number | null }>();

  // 直近5試合。新しいものから
  const recent = await c.env.DB.prepare(
    `SELECT result FROM team_games
      WHERE club_id = ? AND season_id = ? AND result IS NOT NULL
      ORDER BY game_date DESC, game_id DESC LIMIT 5`,
  ).bind(club.id, seasonId).all<{ result: number }>();

  // Elo は直前のスナップショットを読む（詳細設計 1.4）。**その場で計算しない**
  const rating = await c.env.DB.prepare(
    `SELECT elo, games_played FROM team_ratings
      WHERE club_id = ? ORDER BY as_of_date DESC LIMIT 1`,
  ).bind(club.id).first<{ elo: number; games_played: number }>();

  // このクラブの試合に対する的中率。**確定済みの予測のみ**（絶対ルール2）
  const accuracy = await c.env.DB.prepare(
    `SELECT COUNT(*) AS n, AVG(r.is_correct) AS rate
       FROM prediction_results r
       JOIN team_games tg ON tg.game_id = r.game_id
      WHERE tg.club_id = ? AND tg.season_id = ? AND r.outcome <> 'VOID'`,
  ).bind(club.id, seasonId).first<{ n: number; rate: number | null }>();

  const history = await c.env.DB.prepare(
    `SELECT tg.game_id, tg.game_date, tg.is_home, tg.result, tg.margin,
            tg.opponent_id, oc.slug AS opponent_slug,
            os.name AS opponent_name, os.short_name AS opponent_short,
            p.home_win_prob, r.is_correct
       FROM team_games tg
       JOIN clubs oc ON oc.id = tg.opponent_id
       LEFT JOIN club_seasons os ON os.club_id = tg.opponent_id AND os.season_id = tg.season_id
       LEFT JOIN predictions p ON p.game_id = tg.game_id AND p.is_final = 1
       LEFT JOIN prediction_results r
              ON r.prediction_id = p.id AND r.outcome <> 'VOID'
      WHERE tg.club_id = ? AND tg.season_id = ? AND tg.result IS NOT NULL
      ORDER BY tg.game_date DESC, tg.game_id DESC
      LIMIT ?`,
  ).bind(club.id, seasonId, limit).all<HistoryRow>();

  const played = record?.played ?? 0;
  return okCached(c, {
    club: {
      clubId: club.id, slug: club.slug,
      name: season?.name ?? null, shortName: season?.short_name ?? null,
    },
    seasonId,
    record: { wins: record?.wins ?? 0, losses: played - (record?.wins ?? 0) },
    last5: recent.results.map((row) => (row.result === 1 ? 'W' : 'L')),
    avgMargin: record?.avg_margin ?? null,
    elo: rating?.elo ?? null,
    // **母数を必ず返す**（要件 8.3）。0件のときは率を返さない — 分母0の率は意味がない
    accuracy: (accuracy?.n ?? 0) > 0
      ? { accuracy: accuracy?.rate ?? null, n: accuracy?.n ?? 0 }
      : { accuracy: null, n: 0 },
    history: history.results.map((row) => {
      const isHome = row.is_home === 1;
      // **「そのクラブから見た」勝率に読み替える**（詳細設計 3.3）
      const ownWinProb = row.home_win_prob === null
        ? null
        : isHome ? row.home_win_prob : 1 - row.home_win_prob;
      // `margin` は自チームから見た得点差（詳細設計 1.3）
      return {
        gameId: row.game_id,
        gameDate: row.game_date,
        isHome,
        opponent: {
          slug: row.opponent_slug,
          name: row.opponent_name, shortName: row.opponent_short,
        },
        ownWinProb,
        won: row.result === 1,
        margin: row.margin,
        // **外れた試合を隠さない**（要件 8.3）。未照合は null
        isCorrect: row.is_correct === null ? null : row.is_correct === 1,
      };
    }),
  }, CACHE.teams);
});
