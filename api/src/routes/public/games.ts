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
  venue_id: string | null; venue_name_at_game: string | null;
  home_primary_venue_id: string | null;
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

/** この予測に使った項目（詳細設計 2.7.2）。**寄与を持たない。** */
type FactorRow = {
  group_key: string; label_ja: string; value_text: string; larger: string | null;
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

/**
 * 実績の率。**閾値を設けない**（要件 8.3 / 詳細設計 3.3）。
 *
 * 3.3 が `pct` に閾値を課すのは**予測値**に対してであり、実績の `6 / 13` は
 * 丸めのない事実である。**試投数が0のときだけ `null`**（分母0の率は定義されない）。
 * **ここで新しい定数を発明しない。**
 */
function actualPct(made: number | null, attempted: number | null): number | null {
  if (made === null || attempted === null || attempted <= 0) return null;
  return made / attempted;
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

type ActualRow = {
  player_id: string; name: string; club_id: string;
  started: number | null; minutes: number | null;
  fg2m: number | null; fg2a: number | null;
  fg3m: number | null; fg3a: number | null;
  ftm: number | null; fta: number | null;
  oreb: number | null; dreb: number | null;
  ast: number | null; tov: number | null; stl: number | null; blk: number | null;
  pf: number | null; fd: number | null;
  plus_minus: number | null; pts: number | null;
  position: string | null;
};

/** 片方が NULL なら合計も NULL。**0 として足さない**（「記録なし」を「0回」にしない）。 */
function add(a: number | null, b: number | null): number | null {
  return a === null || b === null ? null : a + b;
}

/**
 * その試合の実績1人ぶん（詳細設計 3.3 の `playerActuals`）。
 *
 * **予測を参照しない。** 予測が1本も無い試合でも返す — 2026-10-09 時点の本番
 * 11試合すべてがその状態である。
 *
 * **`pts` は取得値をそのまま返す。** 恒等式との一致は取り込みが検証している（1.3）。
 * **`plusMinus` は実績のみ**（要件 6.8.3）。旧年度は NULL になりうる（4.4）。
 */
function actualBox(row: ActualRow) {
  const fgm = add(row.fg2m, row.fg3m);
  const fga = add(row.fg2a, row.fg3a);
  const tsDenominator =
    fga === null || row.fta === null ? null : 2 * (fga + 0.44 * row.fta);
  return {
    playerId: row.player_id,
    name: row.name,
    position: row.position,
    clubId: row.club_id,
    started: row.started === null ? null : row.started === 1,
    summary: {
      min: row.minutes,
      pts: row.pts,
      reb: add(row.oreb, row.dreb),
      ast: row.ast,
    },
    box: {
      fg: { m: fgm, a: fga, pct: actualPct(fgm, fga) },
      fg2: { m: row.fg2m, a: row.fg2a, pct: actualPct(row.fg2m, row.fg2a) },
      fg3: { m: row.fg3m, a: row.fg3a, pct: actualPct(row.fg3m, row.fg3a) },
      ft: { m: row.ftm, a: row.fta, pct: actualPct(row.ftm, row.fta) },
      oreb: row.oreb, dreb: row.dreb,
      ast: row.ast, tov: row.tov, stl: row.stl, blk: row.blk,
      pf: row.pf, fd: row.fd,
      plusMinus: row.plus_minus,
      efgPct: fgm === null || row.fg3m === null
        ? null
        : actualPct(fgm + 0.5 * row.fg3m, fga),
      tsPct: tsDenominator !== null && tsDenominator > 0 && row.pts !== null
        ? row.pts / tsDenominator
        : null,
    },
  };
}

/** 寄与の相対的な強さ。**数値も符号付きの生値も返さない**（詳細設計 2.7）。 */
function strength(contribution: number, largest: number): number {
  if (largest <= 0) return 1;
  const ratio = Math.abs(contribution) / largest;
  return Math.max(1, Math.ceil(ratio * STRENGTH_STEPS));
}

/**
 * その試合がホームクラブの本拠会場で行われたか（詳細設計 1.2）。
 *
 * **`games.is_primary_venue` を読まない。** あの列は DDL の `DEFAULT 1` のまま
 * 全件 1 で、`club_seasons.primary_venue_id` から1回の JOIN で導ける。複製を持つと
 * 「片方だけ古い」という壊れ方が1つ増えるだけである。
 *
 * **クエリは増えない。** この SELECT は当時の表示名のために既に `club_seasons hs` を
 * LEFT JOIN している。
 *
 * **本拠が分からないときは `true` を返す。** 「分からない」と「代替会場である」は
 * 違う。特徴量側は同じ場合に欠損（None）を返す — あちらは学習に入る値で、
 * ここは画面に出す値である。
 */
function isPrimaryVenue(
  game: Pick<GameRow, 'venue_id' | 'home_primary_venue_id'>,
): boolean {
  if (game.home_primary_venue_id === null || game.venue_id === null) return true;
  return game.venue_id === game.home_primary_venue_id;
}

games.get('/games/:gameId', async (c) => {
  const gameId = c.req.param('gameId');
  if (!GAME_ID.test(gameId)) return fail(c, 'BAD_REQUEST', '試合IDの形式が不正');

  // 表示名は `club_seasons`（当時の名称）を使う。`clubs.name` は現在の表示名で、
  // 過去試合に出すと遡って変わる（詳細設計 1.2）
  const game = await c.env.DB.prepare(
    `SELECT g.id, g.season_id, g.league, g.competition, g.game_date, g.tipoff_at, g.status,
            g.home_club_id, g.away_club_id, g.home_score, g.away_score,
            g.venue_id, g.venue_name_at_game, hs.primary_venue_id AS home_primary_venue_id,
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

  // **実績は予測の有無に依存しない**（詳細設計 3.3 の `playerActuals`）。
  // 予測が1本も無い試合でも「この試合の記録」は出す — 2026-10-09 時点の本番
  // 11試合すべてがその状態であり、依存させると画面に何も出ない
  const actuals = finished
    ? await c.env.DB.prepare(
      `SELECT pgs.player_id, p.name, pgs.club_id, pgs.started, pgs.minutes,
                pgs.fg2m, pgs.fg2a, pgs.fg3m, pgs.fg3a, pgs.ftm, pgs.fta,
                pgs.oreb, pgs.dreb, pgs.ast, pgs.tov, pgs.stl, pgs.blk,
                pgs.pf, pgs.fd, pgs.plus_minus, pgs.pts,
                ps.position AS position
           FROM player_game_stats pgs
           JOIN players p ON p.id = pgs.player_id
           LEFT JOIN player_seasons ps
             ON ps.player_id = pgs.player_id AND ps.club_id = pgs.club_id
          WHERE pgs.game_id = ?
          ORDER BY pgs.minutes IS NULL, pgs.minutes DESC, pgs.player_id`,
    ).bind(gameId).all<ActualRow>()
    : null;

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
        ? { name: game.venue_name_at_game ?? game.venue_name, isPrimary: isPrimaryVenue(game) }
        : null,
      homeScore: finished ? game.home_score : null,
      awayScore: finished ? game.away_score : null,
    },
    prediction: null,
    evaluation: null,
    playerPredictions: [],
    // **キーは常に存在する**（試合前は空配列。3.1 / 3.7 の契約）
    playerActuals: (actuals?.results ?? []).map(actualBox),
    recentForm: null,
    modelAccuracy: null,
  };

  if (!prediction) {
    return okCached(c, body, finished ? CACHE.settled : CACHE.pending);
  }

  const [reasons, factors, players, evaluation, model] = await Promise.all([
    c.env.DB.prepare(
      `SELECT group_key, label_ja, value_text, favors, contribution
         FROM prediction_reasons WHERE prediction_id = ? ORDER BY rank`,
    ).bind(prediction.id).all<ReasonRow>(),
    // **この予測に使った項目**（詳細設計 2.7.2）。`reasons` とは別物で、
    // 寄与ではなく「何を見たか」を全列並べる。並びは `rank`（グループ順 → 列順）
    c.env.DB.prepare(
      `SELECT group_key, label_ja, value_text, larger
         FROM prediction_factors WHERE prediction_id = ? ORDER BY rank`,
    ).bind(prediction.id).all<FactorRow>(),
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
    // **`larger` は「値が大きい側」であり「有利な側」ではない**（2.7.2）。
    // 係数が負の列（守備効率）では両者が逆を向く
    factors: (factors.results ?? []).map((f) => ({
      group: f.group_key, label: f.label_ja, value: f.value_text,
      larger: f.larger === 'HOME' || f.larger === 'AWAY' ? f.larger : null,
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
    //
    // **出典は `hit_rate` であり `actual_rate` ではない**（詳細設計 1.6 / 3.3）。
    // あれはホームが勝った割合で、50%未満の帯では的中率と符号が逆になる。
    // **`/results` と同じ出典を使う** — 同じ「結果の対比」が2つの画面で
    // 別の数字を出さない（v1.101 で帯のラベルだけを返していたのを直したのと同じ理由）
    const bucketKey = `${evaluation.prob_bucket * 10}-${evaluation.prob_bucket * 10 + 10}%`;
    const bucket = await c.env.DB.prepare(
      `SELECT n, hit_rate FROM accuracy_summary
        WHERE scope = 'BUCKET' AND scope_key = ? AND model_version = ''`,
    ).bind(bucketKey).first<{ n: number; hit_rate: number | null }>();
    body.evaluation = {
      isCorrect: evaluation.is_correct === null ? null : evaluation.is_correct === 1,
      scoreError: evaluation.score_mae,
      outcome: evaluation.outcome,
      // **`hit_rate` が NULL の帯は返さない**（詳細設計 3.3）。0 として出すと
      // 「1件も当たっていない」という意味になる
      bucketContext: bucket !== null && bucket.hit_rate !== null
        ? {
            bucket: bucketKey, n: bucket.n,
            correct: Math.round(bucket.hit_rate * bucket.n),
            rate: bucket.hit_rate,
          }
        : null,
    };
  }

  return okCached(c, body, prediction.is_final === 1 ? CACHE.settled : CACHE.pending);
});
