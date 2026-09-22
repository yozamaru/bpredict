/**
 * 公開エンドポイント（詳細設計 3.3 / 3.5）。
 *
 * **形状が設計に書かれているものだけを実装している。** `/games?date=` や `/teams` は
 * 応答形状の記述がなく、推測で埋めない（CLAUDE.md「勝手な仕様補完をしない」）。
 */
import { env } from 'cloudflare:test';
import { beforeEach, describe, expect, it } from 'vitest';

import { applyMigrations, get, seedGame } from './helpers';

beforeEach(applyMigrations);

/** 応答の `data` を取り出す。形が違えばテストが落ちる。 */
async function body(res: Response): Promise<Record<string, unknown>> {
  const json: { data?: Record<string, unknown> } = await res.json();
  expect(json.data).toBeTruthy();
  return json.data ?? {};
}

describe('GET /games/:gameId', () => {
  it('試合IDの形式が不正なら D1 に触らず 400', async () => {
    const res = await get('/games/' + encodeURIComponent('../etc/passwd'));
    expect(res.status).toBe(400);
  });

  it('存在しない試合は 404', async () => {
    const res = await get('/games/does-not-exist');
    expect(res.status).toBe(404);
  });

  it('予測がなくてもキーの位置が変わらない', async () => {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    const data = await body(await get(`/games/${s.gameId}`));

    // **試合前後でキーの位置は同じ。** 中身だけが変わる（詳細設計 3.3）
    expect(Object.keys(data).sort()).toEqual(
      ['evaluation', 'game', 'modelAccuracy', 'playerPredictions', 'prediction', 'recentForm'],
    );
    expect(data.prediction).toBeNull();
    expect(data.evaluation).toBeNull();
    expect(data.playerPredictions).toEqual([]);
  });

  it('未実施は、列に値があってもスコアを返さない', async () => {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    // **列が空のままでは関門を検証できていない。** 途中経過が入っていても
    // `status` が FINISHED でなければ出さないことを確かめる
    await env.DB.prepare('UPDATE games SET home_score = 40, away_score = 38 WHERE id = ?')
      .bind(s.gameId).run();
    const game = (await body(await get(`/games/${s.gameId}`))).game as Record<string, unknown>;
    expect(game.status).toBe('SCHEDULED');
    expect(game.homeScore).toBeNull();
    expect(game.awayScore).toBeNull();
  });

  it('未実施はエッジの TTL を短くする（予測が差し替わりうる）', async () => {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    const res = await get(`/games/${s.gameId}`);
    expect(res.headers.get('Cache-Control')).toContain('s-maxage=300');
  });

  it('終了済みはスコアを返し、長めにキャッシュする', async () => {
    const s = await seedGame({ tipoffAt: '2020-01-01T10:05:00Z', status: 'FINISHED' });
    await env.DB.prepare('UPDATE games SET home_score = 88, away_score = 81 WHERE id = ?')
      .bind(s.gameId).run();
    const res = await get(`/games/${s.gameId}`);
    const game = (await body(res)).game as Record<string, unknown>;
    expect(game.homeScore).toBe(88);
    expect(game.awayScore).toBe(81);
    expect(res.headers.get('Cache-Control')).toContain('s-maxage=3600');
  });

  it('表示名は当時の名称（club_seasons）を使う', async () => {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    await env.DB.prepare(
      'INSERT INTO club_seasons (club_id, season_id, name, short_name, league)'
      + " VALUES (?,?,?,?,'PREMIER')",
    ).bind(s.homeId, s.seasonId, '当時のホーム名', '当時H').run();
    const game = (await body(await get(`/games/${s.gameId}`))).game as {
      home: { name: string; shortName: string };
    };
    expect(game.home.name).toBe('当時のホーム名');
    expect(game.home.shortName).toBe('当時H');
  });
});

/** 予測を1本入れる。子テーブルは呼び出し側が必要な分だけ足す。 */
async function seedPrediction(
  s: { gameId: string; seasonId: string; modelVersion: string },
  opts: { isFinal?: boolean } = {},
) {
  const id = `pred-${s.gameId}`;
  await env.DB.prepare(
    `INSERT INTO predictions (id, game_id, season_id, model_version, revision, run_id,
       predicted_at, as_of, data_as_of, home_win_prob, pred_home_score, pred_away_score,
       is_provisional, is_final, is_active, feature_snapshot)
     VALUES (?,?,?,?,1,'run','2026-09-22T00:00:00Z','2026-09-22T10:05:00Z',
             '2026-09-21T12:00:00Z',0.68,84,78,0,?,1,'{}')`,
  ).bind(id, s.gameId, s.seasonId, s.modelVersion, opts.isFinal ? 1 : 0).run();
  return id;
}

describe('GET /games/:gameId（予測あり）', () => {
  it('根拠は段階値で返し、SHAP の生値を出さない', async () => {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    const predictionId = await seedPrediction(s);
    await env.DB.batch([
      env.DB.prepare(
        `INSERT INTO prediction_reasons (prediction_id, rank, group_key, label_ja,
           value_text, favors, contribution, base_value) VALUES (?,1,?,?,?,?,?,0)`,
      ).bind(predictionId, 'TEAM_STRENGTH', 'チーム力の差', '＋82ポイント', 'HOME', 0.8),
      env.DB.prepare(
        `INSERT INTO prediction_reasons (prediction_id, rank, group_key, label_ja,
           value_text, favors, contribution, base_value) VALUES (?,2,?,?,?,?,?,0)`,
      ).bind(predictionId, 'SCHEDULE', 'アウェイの休養', '中0日', 'HOME', 0.2),
    ]);
    const prediction = (await body(await get(`/games/${s.gameId}`))).prediction as {
      reasons: { strength: number; label: string }[];
    };
    expect(prediction.reasons).toHaveLength(2);
    for (const reason of prediction.reasons) {
      expect(reason.strength).toBeGreaterThanOrEqual(1);
      expect(reason.strength).toBeLessThanOrEqual(4);
      expect(reason).not.toHaveProperty('contribution');
    }
    // 寄与が大きい方が強い
    expect(prediction.reasons[0]!.strength).toBeGreaterThan(prediction.reasons[1]!.strength);
  });

  it('出場確率が0.5未満の選手を含めない', async () => {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    const predictionId = await seedPrediction(s);
    const insert = (pid: string, avail: number) => env.DB.prepare(
      `INSERT INTO player_predictions (id, prediction_id, game_id, player_id, club_id,
         model_version, revision, predicted_at, avail_prob, pred_minutes,
         pred_fg2a, pred_fg3a, pred_fta, pred_fg2_pct, pred_fg3_pct, pred_ft_pct,
         pred_oreb, pred_dreb, pred_ast, pred_tov, pred_stl, pred_blk, pred_pf, pred_fd)
       VALUES (?,?,?,?,?,?,1,'2026-09-22T00:00:00Z',?,30,
               8,5,4,0.5,0.4,0.8, 1,3,6,2,1,0.3,2,3)`,
    ).bind(pid, predictionId, s.gameId, s.playerId, s.homeId, s.modelVersion, avail);
    // **両方入れないと閾値を検証できていない。** 高い方だけだと、閾値を外しても通る
    await env.DB.prepare('INSERT INTO players (id, name) VALUES (?,?)')
      .bind(`${s.playerId}-bench`, '架空 控え選手').run();
    await insert('pp-high', 0.95).run();
    await env.DB.prepare(
      `INSERT INTO player_predictions (id, prediction_id, game_id, player_id, club_id,
         model_version, revision, predicted_at, avail_prob, pred_minutes,
         pred_fg2a, pred_fg3a, pred_fta, pred_fg2_pct, pred_fg3_pct, pred_ft_pct,
         pred_oreb, pred_dreb, pred_ast, pred_tov, pred_stl, pred_blk, pred_pf, pred_fd)
       VALUES ('pp-low',?,?,?,?,?,1,'2026-09-22T00:00:00Z',0.2,4,
               1,0,0,0.5,0,0, 0,0,0,0,0,0,0,0)`,
    ).bind(predictionId, s.gameId, `${s.playerId}-bench`, s.homeId, s.modelVersion).run();

    const data = await body(await get(`/games/${s.gameId}`));
    const players = data.playerPredictions as { availProb: number; playerId: string }[];
    expect(players).toHaveLength(1);
    expect(players[0]!.availProb).toBe(0.95);
    expect(players.map((p) => p.playerId)).not.toContain(`${s.playerId}-bench`);
  });

  it('成功数と得点を導出し、試投数が閾値未満の率は出さない', async () => {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    const predictionId = await seedPrediction(s);
    await env.DB.prepare(
      `INSERT INTO player_predictions (id, prediction_id, game_id, player_id, club_id,
         model_version, revision, predicted_at, avail_prob, pred_minutes,
         pred_fg2a, pred_fg3a, pred_fta, pred_fg2_pct, pred_fg3_pct, pred_ft_pct,
         pred_oreb, pred_dreb, pred_ast, pred_tov, pred_stl, pred_blk, pred_pf, pred_fd)
       VALUES ('pp-1',?,?,?,?,?,1,'2026-09-22T00:00:00Z',0.9,31.2,
               8,4,2, 0.5,0.25,0.5, 0.6,2.5,6.1,2.2,1.1,0.3,2.4,3.1)`,
    ).bind(predictionId, s.gameId, s.playerId, s.homeId, s.modelVersion).run();

    const players = (await body(await get(`/games/${s.gameId}`))).playerPredictions as {
      summary: { pts: number; reb: number };
      box: { fg: { m: number; a: number; pct: number | null }; ft: { a: number; pct: number | null } };
    }[];
    const p = players[0]!;
    // 2FGM=4 / 3FGM=1 / FTM=1 → 得点 = 8 + 3 + 1 = 12（恒等式で導出）
    expect(p.summary.pts).toBeCloseTo(12, 6);
    expect(p.summary.reb).toBeCloseTo(3.1, 6);
    expect(p.box.fg.m).toBeCloseTo(5, 6);
    expect(p.box.fg.a).toBeCloseTo(12, 6);
    expect(p.box.fg.pct).toBeCloseTo(5 / 12, 6);
    // FT は試投数2で閾値3を下回る → 率を出さない（要件 6.8.2）
    expect(p.box.ft.a).toBe(2);
    expect(p.box.ft.pct).toBeNull();
  });

  it('外した試合でも bucketContext を返す', async () => {
    const s = await seedGame({ tipoffAt: '2020-01-01T10:05:00Z', status: 'FINISHED' });
    const predictionId = await seedPrediction(s, { isFinal: true });
    await env.DB.prepare('DELETE FROM accuracy_summary').run();
    await env.DB.batch([
      env.DB.prepare(
        `INSERT INTO prediction_results (prediction_id, game_id, season_id, model_version,
           home_win_prob, prob_bucket, outcome, predicted_home_win, actual_home_win,
           is_correct, brier, score_mae, was_provisional)
         VALUES (?,?,?,?,0.68,6,'LOSS',1,0,0,0.4624,5,0)`,
      ).bind(predictionId, s.gameId, s.seasonId, s.modelVersion),
      env.DB.prepare(
        `INSERT INTO accuracy_summary (scope, scope_key, model_version, n, accuracy, brier,
           actual_rate) VALUES ('BUCKET','60-70%','',42,0.65,0.21,?)`,
      ).bind(29 / 42),
    ]);

    const evaluation = (await body(await get(`/games/${s.gameId}`))).evaluation as {
      isCorrect: boolean; bucketContext: { bucket: string; n: number; correct: number };
    };
    expect(evaluation.isCorrect).toBe(false);
    // **外れても隠さない。** その確率帯の通算を併記する（基本設計 5.2）
    expect(evaluation.bucketContext.bucket).toBe('60-70%');
    expect(evaluation.bucketContext.n).toBe(42);
    expect(evaluation.bucketContext.correct).toBe(29);
  });
});

describe('GET /accuracy', () => {
  it('データがなくても5系統のキーを返す', async () => {
    await env.DB.prepare('DELETE FROM accuracy_summary').run();
    const data = await body(await get('/accuracy'));
    expect(Object.keys(data).sort()).toEqual(
      ['byModel', 'byProvisional', 'bySeason', 'calibration', 'overall'],
    );
    expect(data.overall).toBeNull();
    expect(data.calibration).toEqual([]);
  });

  it('accuracy_summary から読む', async () => {
    // 同一ファイル内で行が残るため、主キーの衝突を避けて先に空にする
    await env.DB.prepare('DELETE FROM accuracy_summary').run();
    await env.DB.batch([
      env.DB.prepare(
        'INSERT INTO accuracy_summary (scope, scope_key, model_version, n, accuracy, brier,'
        + ' actual_rate, baseline_accuracy) VALUES (?,?,?,?,?,?,?,?)',
      ).bind('OVERALL', 'all', '', 312, 0.682, 0.204, null, 0.601),
      env.DB.prepare(
        'INSERT INTO accuracy_summary (scope, scope_key, model_version, n, accuracy, brier,'
        + ' actual_rate) VALUES (?,?,?,?,?,?,?)',
      ).bind('BUCKET', '60-70%', '', 88, 0.65, 0.21, 0.636),
    ]);
    const data = await body(await get('/accuracy'));
    expect(data.overall).toEqual({
      accuracy: 0.682, brier: 0.204, n: 312, baselineAccuracy: 0.601,
    });
    // **母数を必ず添える**（要件 8.3）
    expect(data.calibration).toEqual([
      { bucket: '60-70%', predicted: 0.65, actual: 0.636, n: 88 },
    ]);
  });

  it('キャッシュを効かせる（指定漏れで D1 に直撃させない）', async () => {
    const res = await get('/accuracy');
    expect(res.headers.get('Cache-Control')).toBe(
      'public, max-age=60, s-maxage=3600, stale-while-revalidate=86400',
    );
  });
});

describe('公開エンドポイントに認証を要求しない', () => {
  it.each(['/accuracy', '/games/any-id'])('%s は Bearer なしで 401 にならない', async (path) => {
    const res = await get(path, { token: null });
    expect(res.status).not.toBe(401);
  });
});
