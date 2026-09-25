/**
 * `GET /games?date=` と `GET /results?date=`（詳細設計 3.3）。
 *
 * **`GET /games?date=` の応答は `today.json` の正本でもある**（3.7）。静的JSON 専用の
 * スキーマを作らないため、この形を両方が使う。
 *
 * **シーズン範囲外の日付は D1 の試合検索まで進めない**（要件 4.2）。範囲外を弾かないと
 * URL 空間が無限になり、クローラの総当たりで無料枠が枯渇する。
 */
import { Hono } from 'hono';

import { CACHE } from '../../lib/cache';
import { fail, okCached } from '../../lib/http';
import { isWithinSeason, parseDate } from '../../lib/params';

export type Env = { DB: D1Database };

export const schedule = new Hono<{ Bindings: Env }>();

type ListRow = {
  id: string; season_id: string; competition: string; game_date: string;
  tipoff_at: string; status: string;
  home_club_id: string; away_club_id: string;
  home_score: number | null; away_score: number | null;
  home_slug: string; away_slug: string;
  home_name: string | null; away_name: string | null;
  home_short: string | null; away_short: string | null;
  model_version: string | null;
  home_win_prob: number | null;
  pred_home_score: number | null; pred_away_score: number | null;
  is_provisional: number | null; is_final: number | null;
  is_correct: number | null; score_mae: number | null;
  prob_bucket: number | null;
};

/**
 * 一覧の1件。**表示名は `club_seasons`（当時の名称）を使う。**
 * `clubs.name` は現在の表示名で、過去試合に出すと遡って変わる（詳細設計 1.2）。
 */
function clubs(row: ListRow) {
  return {
    home: {
      clubId: row.home_club_id, slug: row.home_slug,
      name: row.home_name, shortName: row.home_short,
    },
    away: {
      clubId: row.away_club_id, slug: row.away_slug,
      name: row.away_name, shortName: row.away_short,
    },
  };
}

/**
 * 一覧が読む共通のクエリ。
 *
 * **予測は `is_final = 1` を優先し、なければ `is_active = 1` を出す。**
 * 的中率の算出は確定行のみを使う（絶対ルール2）が、一覧の表示は試合前も要る。
 *
 * **1試合につき1行に畳む**（相関サブクエリではなく LEFT JOIN + 窓関数を使わず、
 * `predictions` を1本に絞ってから結合する）。`is_active = 1` は部分ユニーク
 * インデックスで1試合1本、`is_final = 1` も1本であるため、両方を足しても
 * 最大2行にしかならない（詳細設計 1.5）。
 */
const LIST_SQL = `
  SELECT g.id, g.season_id, g.competition, g.game_date, g.tipoff_at, g.status,
         g.home_club_id, g.away_club_id, g.home_score, g.away_score,
         hc.slug AS home_slug, ac.slug AS away_slug,
         hs.name AS home_name, as_.name AS away_name,
         hs.short_name AS home_short, as_.short_name AS away_short,
         p.model_version, p.home_win_prob, p.pred_home_score, p.pred_away_score,
         p.is_provisional, p.is_final,
         r.is_correct, r.score_mae, r.prob_bucket
    FROM games g
    JOIN clubs hc ON hc.id = g.home_club_id
    JOIN clubs ac ON ac.id = g.away_club_id
    LEFT JOIN club_seasons hs ON hs.club_id = g.home_club_id AND hs.season_id = g.season_id
    LEFT JOIN club_seasons as_ ON as_.club_id = g.away_club_id AND as_.season_id = g.season_id
    LEFT JOIN predictions p ON p.id = (
      SELECT id FROM predictions
       WHERE game_id = g.id AND (is_final = 1 OR is_active = 1)
       ORDER BY is_final DESC, revision DESC LIMIT 1)
    LEFT JOIN prediction_results r
           ON r.prediction_id = p.id AND r.outcome <> 'VOID'
   WHERE g.game_date = ?
   ORDER BY g.tipoff_at, g.id`;

/** 確率帯のラベル（`prob_bucket` は 0〜9）。「60-70%」の形にする */
function bucketLabel(bucket: number): string {
  const lower = Math.min(Math.max(bucket, 0), 9) * 10;
  return `${lower}-${lower + 10}%`;
}

async function overallAccuracy(db: D1Database) {
  // モデル横断の集計では `model_version` に空文字が入る（詳細設計 1.6）
  const row = await db.prepare(
    `SELECT n, accuracy, brier FROM accuracy_summary
      WHERE scope = 'OVERALL' AND model_version = '' LIMIT 1`,
  ).first<{ n: number; accuracy: number; brier: number }>();
  return row ? { accuracy: row.accuracy, brier: row.brier, n: row.n } : null;
}

/** 日付を検証して 400 / 404 を返す。**通ったときだけ試合を検索する** */
async function checkedDate(
  db: D1Database, raw: string | undefined,
): Promise<{ date: string } | { error: 'BAD_REQUEST' | 'NOT_FOUND'; message: string }> {
  const date = parseDate(raw);
  if (date === null) {
    return { error: 'BAD_REQUEST', message: 'date は YYYY-MM-DD の実在日付で指定する' };
  }
  if (!(await isWithinSeason(db, date))) {
    // **範囲外は 404。** URL 空間を有限に保つ（要件 4.2）
    return { error: 'NOT_FOUND', message: 'シーズンの範囲外の日付' };
  }
  return { date };
}

schedule.get('/games', async (c) => {
  const checked = await checkedDate(c.env.DB, c.req.query('date'));
  if ('error' in checked) return fail(c, checked.error, checked.message);

  const rows = await c.env.DB.prepare(LIST_SQL).bind(checked.date).all<ListRow>();
  const games = rows.results.map((row) => ({
    gameId: row.id,
    // **UTC のまま返す。** JST への変換は画面で行う（CLAUDE.md 時刻の扱い）
    tipoffAt: row.tipoff_at,
    status: row.status,
    competition: row.competition,
    ...clubs(row),
    // 予測がまだない試合は null。**試合ごと省略しない** — 日程に載っているのに
    // 予測がない状態は実在し、画面は空状態を出す（要件 8.5）
    prediction: row.home_win_prob === null ? null : {
      homeWinProb: row.home_win_prob,
      predHomeScore: row.pred_home_score,
      predAwayScore: row.pred_away_score,
      isProvisional: row.is_provisional === 1,
      isFinal: row.is_final === 1,
      // 序盤の判定は消化試合数を要する。**推測で false を返さない** —
      // 集計が入るまでは null（画面は出さない）
      isEarlySeason: null,
      modelVersion: row.model_version,
    },
  }));

  // 過去日は確定しており長く持たせてよい。当日・未来は予測が差し替わる
  const settled = games.length > 0 && games.every((g) => g.status !== 'SCHEDULED');
  return okCached(
    c,
    { gameDate: checked.date, games, accuracy: await overallAccuracy(c.env.DB) },
    settled ? CACHE.settled : CACHE.pending,
  );
});

schedule.get('/results', async (c) => {
  const checked = await checkedDate(c.env.DB, c.req.query('date'));
  if ('error' in checked) return fail(c, checked.error, checked.message);

  const rows = await c.env.DB.prepare(LIST_SQL).bind(checked.date).all<ListRow>();
  const results = rows.results
    // 終了していて、照合済みの予測がある試合だけ。**`VOID` は返さない**
    // （的中率の母数から除外される予測であり、対比の対象がない。詳細設計 1.6）
    .filter((row) => row.status === 'FINISHED' && row.is_correct !== null)
    .map((row) => ({
      gameId: row.id,
      tipoffAt: row.tipoff_at,
      ...clubs(row),
      homeScore: row.home_score,
      awayScore: row.away_score,
      prediction: {
        homeWinProb: row.home_win_prob,
        predHomeScore: row.pred_home_score,
        predAwayScore: row.pred_away_score,
        modelVersion: row.model_version,
      },
      // **外れた試合でも同じ形で返す**（要件 8.3）
      evaluation: {
        isCorrect: row.is_correct === 1,
        scoreError: row.score_mae,
        bucket: row.prob_bucket === null ? null : bucketLabel(row.prob_bucket),
      },
    }));

  return okCached(c, { gameDate: checked.date, results }, CACHE.settled);
});
