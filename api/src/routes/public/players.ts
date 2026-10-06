/**
 * `GET /players/:playerId`（詳細設計 3.3）。
 *
 * **出すのは集計値だけである**（要件 3.1.1）。試合ごとの記録の一覧を返さない —
 * 147,082行の `player_game_stats` を選手・季をまたいで並べると、要件 4.5.3 が
 * 残存リスクとして挙げる「体系的・網羅的な複製」がその粒度のまま公開される。
 *
 * **予測値を1つも返さない。** この口が返すのはすべて実績である。
 *
 * **実行時に集計しない。** `player_stat_summary` を読む（基本設計 3.2）。
 */
import { Hono } from 'hono';

import { CACHE } from '../../lib/cache';
import { fail, okCached } from '../../lib/http';
import { parsePlayerId } from '../../lib/params';
import { playerStatOf, type PlayerStatRow } from '../../lib/stats';

export type Env = { DB: D1Database };

export const players = new Hono<{ Bindings: Env }>();

type Row = PlayerStatRow & {
  season_label: string | null;
  club_slug: string | null;
  club_name: string | null;
  club_short: string | null;
};

players.get('/players/:playerId', async (c) => {
  const playerId = parsePlayerId(c.req.param('playerId'));
  if (playerId === null) return fail(c, 'BAD_REQUEST', 'playerId の形式が不正');

  const player = await c.env.DB.prepare('SELECT id, name FROM players WHERE id = ? LIMIT 1')
    .bind(playerId)
    .first<{ id: string; name: string }>();
  if (!player) return fail(c, 'NOT_FOUND', '選手が見つからない');

  // **クラブ名は `club_seasons`（年度断面）から引く。** `clubs.name` を使うと
  // 過去の季の表示が遡って変わる（詳細設計 1.2）
  const rows = await c.env.DB.prepare(
    `SELECT s.scope, s.scope_key, s.club_id, s.games, s.games_started, s.minutes,
            s.fg2m, s.fg2a, s.fg3m, s.fg3a, s.ftm, s.fta, s.oreb, s.dreb,
            s.ast, s.tov, s.stl, s.blk, s.pf, s.fd, s.pts,
            se.label AS season_label,
            c.slug AS club_slug, cs.name AS club_name, cs.short_name AS club_short
       FROM player_stat_summary s
       LEFT JOIN seasons se ON se.id = s.scope_key
       LEFT JOIN clubs c ON c.id = s.club_id
       LEFT JOIN club_seasons cs ON cs.club_id = s.club_id AND cs.season_id = s.scope_key
      WHERE s.player_id = ?
      ORDER BY s.scope, s.scope_key DESC, s.club_id`,
  )
    .bind(playerId)
    .all<Row>();

  // **集計が1行もなければ 404。** 静的生成の範囲内でも、集計が走る前は空になる
  if (rows.results.length === 0) return fail(c, 'NOT_FOUND', 'この選手の集計がまだない');

  const careerRow = rows.results.find((r) => r.scope === 'CAREER');
  const seasonRows = rows.results.filter((r) => r.scope === 'SEASON');

  // 当季の所属。**背番号とポジションの出典は `player_seasons`**（詳細設計 1.2）
  const current = await c.env.DB.prepare(
    `SELECT ps.season_id, ps.number, ps.position,
            c.slug AS club_slug, cs.name AS club_name
       FROM player_seasons ps
       JOIN clubs c ON c.id = ps.club_id
       LEFT JOIN club_seasons cs ON cs.club_id = ps.club_id AND cs.season_id = ps.season_id
       JOIN seasons se ON se.id = ps.season_id
      WHERE ps.player_id = ?
      ORDER BY se.start_date DESC
      LIMIT 1`,
  )
    .bind(playerId)
    .first<{
      season_id: string;
      number: string | null;
      position: string | null;
      club_slug: string;
      club_name: string | null;
    }>();

  return okCached(
    c,
    {
      player: { playerId: player.id, name: player.name },
      current: current
        ? {
            seasonId: current.season_id,
            clubSlug: current.club_slug,
            clubName: current.club_name,
            number: current.number,
            position: current.position,
          }
        : null,
      career: careerRow === undefined ? null : playerStatOf(careerRow),
      // **季中に移籍した季は2行になる**（クラブ別に持つため。詳細設計 1.9）
      seasons: seasonRows.map((row) => ({
        seasonId: row.scope_key,
        label: row.season_label,
        club: { slug: row.club_slug, name: row.club_name, shortName: row.club_short },
        ...playerStatOf(row),
      })),
    },
    CACHE.teams,
  );
});
