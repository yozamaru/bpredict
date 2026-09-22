/**
 * `GET /games/:gameId` — 試合詳細（詳細設計 3.3）。
 *
 * **レスポンス形状は試合前後で同一にする。** `game` は日程・会場・結果といった事実のみを
 * 持ち、`prediction` は `game` の内側ではなく `data` 直下に置く。試合前後で変わるのは
 * 各フィールドの中身であって、キーの位置ではない。キーの位置が状態で動くと、
 * クライアントが状態ごとに別のパスを持つことになり、片方だけ壊れる不具合が出る。
 */
import { Hono } from 'hono';

import { CACHE } from '../../lib/cache';
import { fail, okCached } from '../../lib/http';

export type Env = { DB: D1Database };

export const games = new Hono<{ Bindings: Env }>();

/** 内部スキーマの `ID` と同じ制約（詳細設計 3.2）。範囲外は D1 に触らず 400。 */
const GAME_ID = /^[A-Za-z0-9._-]{1,64}$/;

/** `pct` を返す試投数の閾値（詳細設計 3.3）。下回る場合は分数のみを出す。 */
const PCT_THRESHOLD = { fg: 4, fg2: 3, fg3: 3, ft: 3 } as const;

/** SHAP の生値を返さない。1〜4 の段階値に畳む（詳細設計 2.7）。 */
const STRENGTH_STEPS = 4;

type GameRow = {
  id: string; season_id: string; league: string; competition: string;
  game_date: string; tipoff_at: string; status: string;
  home_club_id: string; away_club_id: string;
  home_score: number | null; away_score: number | null;
  venue_id: string | null; venue_name_at_game: string | null; is_primary_venue: number;
  home_slug: string; away_slug: string;
  home_name: string | null; away_name: string | null;
  home_short: string | null; away_short: string | null;
  venue_name: string | null;
};

type PredictionRow = {
  id: string; model_version: string; home_win_prob: number;
  pred_home_score: number | null; pred_away_score: number | null;
  is_provisional: number; is_final: number;
};

type ReasonRow = {
  group_key: string; label_ja: string; value_text: string; favors: string; contribution: number;
};

type PlayerRow = {
  player_id: string; name: string; club_id: string; avail_prob: number;
  pred_minutes: number;
  pred_fg2a: number; pred_fg3a: number; pred_fta: number;
  pred_fg2_pct: number; pred_fg3_pct: number; pred_ft_pct: number;
  pred_oreb: number; pred_dreb: number; pred_ast: number; pred_tov: number;
  pred_stl: number; pred_blk: number; pred_pf: number; pred_fd: number;
  err_minutes: number | null; err_pts: number | null;
  err_reb: number | null; err_ast: number | null;
  position: string | null;
};

/** 試投数が閾値未満なら率を出さない。**存在しない精度を主張しない**（要件 6.8.2）。 */
function pct(made: number, attempted: number, threshold: number): number | null {
  return attempted >= threshold ? made / attempted : null;
}

function box(row: PlayerRow) {
  // 成功数は「率 × 試投数」の導出値。独立に持たない（詳細設計 1.5）
  const fg2m = row.pred_fg2_pct * row.pred_fg2a;
  const fg3m = row.pred_fg3_pct * row.pred_fg3a;
  const ftm = row.pred_ft_pct * row.pred_fta;
  const fgm = fg2m + fg3m;
  const fga = row.pred_fg2a + row.pred_fg3a;
  // 得点は恒等式で導出する。独立に予測しない（要件 6.8.2）
  const pts = fg2m * 2 + fg3m * 3 + ftm;
  const reb = row.pred_oreb + row.pred_dreb;
  const tsDenominator = 2 * (fga + 0.44 * row.pred_fta);
  return {
    summary: { min: row.pred_minutes, pts, reb, ast: row.pred_ast },
    error: {
      min: row.err_minutes, pts: row.err_pts, reb: row.err_reb, ast: row.err_ast,
    },
    box: {
      fg: { m: fgm, a: fga, pct: pct(fgm, fga, PCT_THRESHOLD.fg) },
      fg2: { m: fg2m, a: row.pred_fg2a, pct: pct(fg2m, row.pred_fg2a, PCT_THRESHOLD.fg2) },
      fg3: { m: fg3m, a: row.pred_fg3a, pct: pct(fg3m, row.pred_fg3a, PCT_THRESHOLD.fg3) },
      ft: { m: ftm, a: row.pred_fta, pct: pct(ftm, row.pred_fta, PCT_THRESHOLD.ft) },
      oreb: row.pred_oreb, dreb: row.pred_dreb,
      ast: row.pred_ast, tov: row.pred_tov, stl: row.pred_stl, blk: row.pred_blk,
      pf: row.pred_pf, fd: row.pred_fd,
      // EFG% / TS% も導出値。試投数が閾値未満なら出さない
      efgPct: pct(fgm + 0.5 * fg3m, fga, PCT_THRESHOLD.fg),
      tsPct: tsDenominator > 0 && fga >= PCT_THRESHOLD.fg ? pts / tsDenominator : null,
    },
  };
}

/** 寄与の相対的な強さ。**数値も符号付きの生値も返さない**（詳細設計 2.7）。 */
function strength(contribution: number, largest: number): number {
  if (largest <= 0) return 1;
  const ratio = Math.abs(contribution) / largest;
  return Math.max(1, Math.ceil(ratio * STRENGTH_STEPS));
}

games.get('/games/:gameId', async (c) => {
  const gameId = c.req.param('gameId');
  if (!GAME_ID.test(gameId)) return fail(c, 'BAD_REQUEST', '試合IDの形式が不正');

  // 表示名は `club_seasons`（当時の名称）を使う。`clubs.name` は現在の表示名で、
  // 過去試合に出すと遡って変わる（詳細設計 1.2）
  const game = await c.env.DB.prepare(
    `SELECT g.id, g.season_id, g.league, g.competition, g.game_date, g.tipoff_at, g.status,
            g.home_club_id, g.away_club_id, g.home_score, g.away_score,
            g.venue_id, g.venue_name_at_game, g.is_primary_venue,
            hc.slug AS home_slug, ac.slug AS away_slug,
            hs.name AS home_name, as_.name AS away_name,
            hs.short_name AS home_short, as_.short_name AS away_short,
            v.name AS venue_name
       FROM games g
       JOIN clubs hc ON hc.id = g.home_club_id
       JOIN clubs ac ON ac.id = g.away_club_id
       LEFT JOIN club_seasons hs ON hs.club_id = g.home_club_id AND hs.season_id = g.season_id
       LEFT JOIN club_seasons as_ ON as_.club_id = g.away_club_id AND as_.season_id = g.season_id
       LEFT JOIN venues v ON v.id = g.venue_id
      WHERE g.id = ?`,
  ).bind(gameId).first<GameRow>();
  if (!game) return fail(c, 'NOT_FOUND', '試合が見つからない');

  // 的中率の算出は `is_final = 1` の行のみを使う（絶対ルール2）。試合前は
  // 有効な予測（`is_active = 1`）を出す
  const prediction = await c.env.DB.prepare(
    `SELECT id, model_version, home_win_prob, pred_home_score, pred_away_score,
            is_provisional, is_final
       FROM predictions WHERE game_id = ? AND (is_final = 1 OR is_active = 1)
      ORDER BY is_final DESC, revision DESC LIMIT 1`,
  ).bind(gameId).first<PredictionRow>();

  const finished = game.status === 'FINISHED';
  const body: Record<string, unknown> = {
    game: {
      gameId: game.id,
      tipoffAt: game.tipoff_at,
      league: game.league,
      competition: game.competition,
      status: game.status,
      home: {
        clubId: game.home_club_id, slug: game.home_slug,
        name: game.home_name, shortName: game.home_short,
      },
      away: {
        clubId: game.away_club_id, slug: game.away_slug,
        name: game.away_name, shortName: game.away_short,
      },
      // 当時の名称を優先する。無ければ現在の表示名にフォールバックする（詳細設計 1.2）
      venue: game.venue_id
        ? { name: game.venue_name_at_game ?? game.venue_name, isPrimary: game.is_primary_venue === 1 }
        : null,
      homeScore: finished ? game.home_score : null,
      awayScore: finished ? game.away_score : null,
    },
    prediction: null,
    evaluation: null,
    playerPredictions: [],
    recentForm: null,
    modelAccuracy: null,
  };

  if (!prediction) {
    return okCached(c, body, finished ? CACHE.settled : CACHE.pending);
  }

  const [reasons, players, evaluation, model] = await Promise.all([
    c.env.DB.prepare(
      `SELECT group_key, label_ja, value_text, favors, contribution
         FROM prediction_reasons WHERE prediction_id = ? ORDER BY rank`,
    ).bind(prediction.id).all<ReasonRow>(),
    // `availProb < 0.5` の選手は含めない（要件 6.8.4）
    c.env.DB.prepare(
      `SELECT pp.player_id, p.name, pp.club_id, pp.avail_prob, pp.pred_minutes,
              pp.pred_fg2a, pp.pred_fg3a, pp.pred_fta,
              pp.pred_fg2_pct, pp.pred_fg3_pct, pp.pred_ft_pct,
              pp.pred_oreb, pp.pred_dreb, pp.pred_ast, pp.pred_tov,
              pp.pred_stl, pp.pred_blk, pp.pred_pf, pp.pred_fd,
              pp.err_minutes, pp.err_pts, pp.err_reb, pp.err_ast,
              ps.position AS position
         FROM player_predictions pp
         JOIN players p ON p.id = pp.player_id
         LEFT JOIN player_seasons ps
           ON ps.player_id = pp.player_id AND ps.club_id = pp.club_id
        WHERE pp.prediction_id = ? AND pp.avail_prob >= 0.5
        ORDER BY pp.pred_minutes DESC`,
    ).bind(prediction.id).all<PlayerRow>(),
    c.env.DB.prepare(
      `SELECT is_correct, score_mae, prob_bucket, outcome
         FROM prediction_results WHERE prediction_id = ?`,
    ).bind(prediction.id).first<{
      is_correct: number | null; score_mae: number | null;
      prob_bucket: number; outcome: string;
    }>(),
    c.env.DB.prepare(
      `SELECT n, accuracy, brier FROM accuracy_summary
        WHERE scope = 'MODEL' AND scope_key = ?`,
    ).bind(prediction.model_version).first<{ n: number; accuracy: number; brier: number }>(),
  ]);

  const reasonRows = reasons.results ?? [];
  const largest = Math.max(0, ...reasonRows.map((r) => Math.abs(r.contribution)));
  body.prediction = {
    homeWinProb: prediction.home_win_prob,
    predHomeScore: prediction.pred_home_score,
    predAwayScore: prediction.pred_away_score,
    isProvisional: prediction.is_provisional === 1,
    isFinal: prediction.is_final === 1,
    modelVersion: prediction.model_version,
    reasons: reasonRows.map((r) => ({
      group: r.group_key, label: r.label_ja, value: r.value_text,
      favors: r.favors, strength: strength(r.contribution, largest),
    })),
  };
  body.playerPredictions = (players.results ?? []).map((row) => ({
    playerId: row.player_id, name: row.name, position: row.position, clubId: row.club_id,
    availProb: row.avail_prob, ...box(row),
  }));
  body.modelAccuracy = model
    ? { version: prediction.model_version, accuracy: model.accuracy, brier: model.brier, n: model.n }
    : null;

  if (evaluation) {
    // **外れた試合でも `bucketContext` を返す。** その確率帯の通算的中率を併記して
    // 較正が取れていること自体を信頼の材料にする（基本設計 5.2）
    const bucketKey = `${evaluation.prob_bucket * 10}-${evaluation.prob_bucket * 10 + 10}%`;
    const bucket = await c.env.DB.prepare(
      `SELECT n, accuracy, actual_rate FROM accuracy_summary
        WHERE scope = 'BUCKET' AND scope_key = ? AND model_version = ''`,
    ).bind(bucketKey).first<{ n: number; accuracy: number; actual_rate: number | null }>();
    body.evaluation = {
      isCorrect: evaluation.is_correct === null ? null : evaluation.is_correct === 1,
      scoreError: evaluation.score_mae,
      outcome: evaluation.outcome,
      bucketContext: bucket
        ? {
            bucket: bucketKey, n: bucket.n,
            correct: Math.round((bucket.actual_rate ?? 0) * bucket.n),
            rate: bucket.actual_rate,
          }
        : null,
    };
  }

  return okCached(c, body, prediction.is_final === 1 ? CACHE.settled : CACHE.pending);
});
