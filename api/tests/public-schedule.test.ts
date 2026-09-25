/**
 * `GET /games?date=` `GET /results?date=` `GET /teams` `GET /teams/:slug`（詳細設計 3.3）。
 *
 * **`GET /games?date=` の応答は `today.json` の正本でもある**（3.7）。ここで形を変えると
 * 静的JSON 側も変わる。
 */
import { env } from 'cloudflare:test';
import { beforeAll, beforeEach, describe, expect, it } from 'vitest';

import { applyMigrations, get, resetAll, seedGame, type Seed } from './helpers';

beforeAll(async () => { await applyMigrations(); });
beforeEach(async () => { await resetAll(); });

/**
 * **テストごとに一意の日付とシーズンを使う。**
 *
 * `resetAll()` は凍結済み予測（`is_final = 1`）に紐づく行を消せない（トリガで
 * 禁じられている）。そのため試合・シーズンが後続のテストに残り、`game_date` を
 * 共有すると一覧に他のテストの試合が混ざる。**日付を分ければ干渉しない。**
 */
let dayCounter = 0;

function nextDate(): string {
  dayCounter += 1;
  // helpers の seasons は 2026-09-22〜2027-05-02。その内側で日を進める
  const day = String(dayCounter).padStart(2, '0');
  return `2026-10-${day}`;
}

/** その試合を指定日に移し、シーズンの期間もその日を含むようにする */
async function moveTo(s: Seed, date: string) {
  await env.DB.batch([
    env.DB.prepare('UPDATE games SET game_date = ? WHERE id = ?').bind(date, s.gameId),
    env.DB.prepare('UPDATE seasons SET start_date = ?, end_date = ? WHERE id = ?')
      .bind(date, date, s.seasonId),
  ]);
}

/** クラブの年度断面を入れる。**表示名は `club_seasons` が持つ**（詳細設計 1.2） */
async function seedClubSeasons(s: Seed) {
  await env.DB.batch([
    env.DB.prepare(
      `INSERT INTO club_seasons (club_id,season_id,name,short_name,league)
       VALUES (?,?,?,?,'PREMIER'),(?,?,?,?,'PREMIER')`,
    ).bind(s.homeId, s.seasonId, '架空ホームクラブ', '架空H',
           s.awayId, s.seasonId, '架空アウェイクラブ', '架空A'),
  ]);
}

/** 有効な予測を1本入れる（`is_active = 1`）。 */
async function seedPrediction(s: Seed, over: Partial<{
  id: string; homeWinProb: number; isFinal: number; isProvisional: number;
}> = {}) {
  const id = over.id ?? `pred-${s.gameId}`;
  await env.DB.prepare(
    `INSERT INTO predictions
       (id,game_id,season_id,model_version,revision,run_id,predicted_at,as_of,data_as_of,
        home_win_prob,pred_home_score,pred_away_score,is_provisional,is_final,is_active,
        feature_snapshot)
     VALUES (?,?,?,?,1,'run-1','2026-09-22T00:00:00Z','2026-09-22T10:05:00Z',
             '2026-09-21T12:00:00Z',?,84,78,?,?,?,'{}')`,
  ).bind(id, s.gameId, s.seasonId, s.modelVersion,
         over.homeWinProb ?? 0.68, over.isProvisional ?? 0, over.isFinal ?? 0,
         over.isFinal === 1 ? 0 : 1).run();
  return id;
}

describe('GET /games?date=（today.json の正本）', () => {
  it('シーズン範囲内の日付で試合と予測を返す', async () => {
    const date = nextDate();
    const s = await seedGame({ tipoffAt: '2026-09-22T10:05:00Z' });
    await seedClubSeasons(s);
    await seedPrediction(s);
    await moveTo(s, date);

    const res = await get(`/games?date=${date}`, { token: null });
    expect(res.status).toBe(200);
    const body = await res.json<{ data: {
      gameDate: string;
      games: { gameId: string; tipoffAt: string; home: { slug: string; name: string };
               prediction: { homeWinProb: number; isFinal: boolean } | null }[];
    } }>();

    expect(body.data.gameDate).toBe(date);
    expect(body.data.games).toHaveLength(1);
    const game = body.data.games[0]!;
    expect(game.gameId).toBe(s.gameId);
    // **UTC のまま返す。** JST の文字列を作らない（CLAUDE.md 時刻の扱い）
    expect(game.tipoffAt).toBe('2026-09-22T10:05:00Z');
    // 表示名は club_seasons（当時の名称）
    expect(game.home.name).toBe('架空ホームクラブ');
    expect(game.prediction?.homeWinProb).toBeCloseTo(0.68);
  });

  it('予測がまだない試合は prediction が null（試合ごと省略しない）', async () => {
    // 日程に載っているのに予測がない状態は実在する（要件 8.5 の空状態）
    const date = nextDate();
    const s = await seedGame({ tipoffAt: '2026-09-22T10:05:00Z' });
    await seedClubSeasons(s);
    await moveTo(s, date);

    const res = await get(`/games?date=${date}`, { token: null });
    const body = await res.json<{ data: { games: { prediction: unknown }[] } }>();
    expect(body.data.games).toHaveLength(1);
    expect(body.data.games[0]!.prediction).toBeNull();
  });

  it('awayWinProb を返さない（1 - homeWinProb で導出できる冗長値）', async () => {
    const date = nextDate();
    const s = await seedGame({ tipoffAt: '2026-09-22T10:05:00Z' });
    await seedClubSeasons(s);
    await seedPrediction(s);
    await moveTo(s, date);
    const text = await (await get(`/games?date=${date}`, { token: null })).text();
    expect(text).not.toContain('awayWinProb');
  });

  it('tipoffLabel（JST の文字列）を返さない', async () => {
    const date = nextDate();
    const s = await seedGame({ tipoffAt: '2026-09-22T10:05:00Z' });
    await seedClubSeasons(s);
    await moveTo(s, date);
    const text = await (await get(`/games?date=${date}`, { token: null })).text();
    expect(text).not.toContain('tipoffLabel');
  });

  it('試合がない日でも 200 で空配列（エラーにしない）', async () => {
    const s = await seedGame({ tipoffAt: '2026-09-22T10:05:00Z' });
    // シーズンの期間を広げ、試合のない日を範囲内にする
    await env.DB.prepare('UPDATE seasons SET start_date = ?, end_date = ? WHERE id = ?')
      .bind('2026-11-01', '2026-11-30', s.seasonId).run();
    await env.DB.prepare('UPDATE games SET game_date = ? WHERE id = ?')
      .bind('2026-11-01', s.gameId).run();
    const res = await get('/games?date=2026-11-02', { token: null });
    expect(res.status).toBe(200);
    const body = await res.json<{ data: { games: unknown[] } }>();
    expect(body.data.games).toEqual([]);
  });

  it('的中率は母数つきで返す（要件 8.3）', async () => {
    const date = nextDate();
    const s = await seedGame({ tipoffAt: '2026-09-22T10:05:00Z' });
    await moveTo(s, date);
    await env.DB.prepare(
      `INSERT INTO accuracy_summary (scope,scope_key,model_version,n,accuracy,brier)
       VALUES ('OVERALL','all','',312,0.682,0.204)`,
    ).run();
    const body = await (await get(`/games?date=${date}`, { token: null }))
      .json<{ data: { accuracy: { n: number; accuracy: number } | null } }>();
    expect(body.data.accuracy).toEqual({ accuracy: 0.682, brier: 0.204, n: 312 });
  });

  it('予測が0件なら accuracy は null（分母0の率を作らない）', async () => {
    const date = nextDate();
    const s = await seedGame({ tipoffAt: '2026-09-22T10:05:00Z' });
    await moveTo(s, date);
    const body = await (await get(`/games?date=${date}`, { token: null }))
      .json<{ data: { accuracy: unknown } }>();
    expect(body.data.accuracy).toBeNull();
  });
});

describe('日付の検証（URL 空間の有限化。要件 4.2）', () => {
  it.each(['', 'not-a-date', '2026-9-22', '2026-09-32', '2026-02-30', '1999-09-22', '2200-01-01'])(
    'date=%s は 400（D1 に触れる前に弾く）',
    async (value) => {
      const res = await get(`/games?date=${value}`, { token: null });
      expect(res.status).toBe(400);
    },
  );

  it('date を省略しても 400', async () => {
    expect((await get('/games', { token: null })).status).toBe(400);
  });

  it('実在するがシーズン範囲外の日付は 404', async () => {
    const s = await seedGame({ tipoffAt: '2026-09-22T10:05:00Z' });
    await moveTo(s, nextDate());
    // どのシーズンの期間にも入らない日
    const res = await get('/games?date=2026-07-01', { token: null });
    expect(res.status).toBe(404);
  });
});

describe('GET /results?date=', () => {
  /** 終了した試合と照合結果を入れる。 */
  async function seedFinished(over: { isCorrect: number; outcome?: string; date: string }) {
    const s = await seedGame({ tipoffAt: '2026-09-22T10:05:00Z', status: 'FINISHED' });
    await seedClubSeasons(s);
    await moveTo(s, over.date);
    await env.DB.prepare('UPDATE games SET home_score = 88, away_score = 81 WHERE id = ?')
      .bind(s.gameId).run();
    const predictionId = await seedPrediction(s, { isFinal: 1 });
    await env.DB.prepare(
      `INSERT INTO prediction_results
         (prediction_id,game_id,season_id,model_version,home_win_prob,prob_bucket,outcome,
          predicted_home_win,actual_home_win,is_correct,brier,score_mae,was_provisional)
       VALUES (?,?,?,?,0.68,6,?,1,1,?,0.1024,3,0)`,
    ).bind(predictionId, s.gameId, s.seasonId, s.modelVersion,
           over.outcome ?? 'WIN', over.isCorrect).run();
    return s;
  }

  it('的中した試合を実績と予測の対比で返す', async () => {
    const date = nextDate();
    await seedFinished({ isCorrect: 1, date });
    const body = await (await get(`/results?date=${date}`, { token: null }))
      .json<{ data: { results: { homeScore: number;
        evaluation: { isCorrect: boolean; bucket: string | null } }[] } }>();
    expect(body.data.results).toHaveLength(1);
    expect(body.data.results[0]!.homeScore).toBe(88);
    expect(body.data.results[0]!.evaluation.isCorrect).toBe(true);
    // 確率帯は「60-70%」の形（prob_bucket = 6）
    expect(body.data.results[0]!.evaluation.bucket).toBe('60-70%');
  });

  it('**外れた試合も同じ形で返す**（隠さない。要件 8.3）', async () => {
    const date = nextDate();
    await seedFinished({ isCorrect: 0, date });
    const body = await (await get(`/results?date=${date}`, { token: null }))
      .json<{ data: { results: { evaluation: { isCorrect: boolean } }[] } }>();
    expect(body.data.results).toHaveLength(1);
    expect(body.data.results[0]!.evaluation.isCorrect).toBe(false);
  });

  it('VOID（中止・延期）は返さない', async () => {
    const date = nextDate();
    await seedFinished({ isCorrect: 0, outcome: 'VOID', date });
    const body = await (await get(`/results?date=${date}`, { token: null }))
      .json<{ data: { results: unknown[] } }>();
    expect(body.data.results).toEqual([]);
  });

  it('未実施の試合は返さない', async () => {
    const date = nextDate();
    const s = await seedGame({ tipoffAt: '2026-09-22T10:05:00Z' });
    await seedClubSeasons(s);
    await seedPrediction(s);
    await moveTo(s, date);
    const body = await (await get(`/results?date=${date}`, { token: null }))
      .json<{ data: { results: unknown[] } }>();
    expect(body.data.results).toEqual([]);
  });
});

describe('GET /teams と GET /teams/:slug', () => {
  /**
   * 一覧から slug を引く。**`slug` は `clubs` が持つ恒久の識別子**で、試合IDからは
   * 導けない（詳細設計 1.1）。表示名で引き当てる。
   */
  async function slugOf(name: string): Promise<string> {
    const list = await (await get('/teams', { token: null }))
      .json<{ data: { teams: { slug: string; name: string }[] } }>();
    const found = list.data.teams.find((t) => t.name === name);
    expect(found, `${name} が一覧にない`).toBeDefined();
    return found!.slug;
  }

  it('当季のクラブ一覧を返す', async () => {
    const s = await seedGame({ tipoffAt: '2026-09-22T10:05:00Z' });
    await seedClubSeasons(s);
    await moveTo(s, nextDate());
    const body = await (await get('/teams', { token: null }))
      .json<{ data: { seasonId: string | null; teams: { slug: string; name: string }[] } }>();
    // 当季 = 最も新しく始まったシーズン。**「いま」を見ない**（params.latestSeasonId）
    expect(body.data.seasonId).toBe(s.seasonId);
    expect(body.data.teams.map((t) => t.name).sort())
      .toEqual(['架空アウェイクラブ', '架空ホームクラブ']);
  });

  it('クラブがなければ 404、slug の形式が不正なら 400', async () => {
    expect((await get('/teams/no-such-club', { token: null })).status).toBe(404);
    expect((await get('/teams/BadSlug', { token: null })).status).toBe(400);
    expect((await get('/teams/%E6%97%A5%E6%9C%AC', { token: null })).status).toBe(400);
  });

  it('成績・直近5試合・Elo・的中率・履歴を返す', async () => {
    const date = nextDate();
    const s = await seedGame({ tipoffAt: '2026-09-22T10:05:00Z', status: 'FINISHED' });
    await seedClubSeasons(s);
    await moveTo(s, date);
    await env.DB.batch([
      env.DB.prepare(
        `INSERT INTO team_games (game_id,club_id,opponent_id,season_id,game_date,is_home,
           competition,result,margin) VALUES (?,?,?,?,?,1,'REGULAR',1,7)`,
      ).bind(s.gameId, s.homeId, s.awayId, s.seasonId, date),
      env.DB.prepare(
        `INSERT INTO team_ratings (club_id,as_of_date,season_id,elo,games_played)
         VALUES (?,?,?,1582.5,20)`,
      ).bind(s.homeId, date, s.seasonId),
    ]);
    const predictionId = await seedPrediction(s, { isFinal: 1 });
    await env.DB.prepare(
      `INSERT INTO prediction_results
         (prediction_id,game_id,season_id,model_version,home_win_prob,prob_bucket,outcome,
          predicted_home_win,actual_home_win,is_correct,brier,score_mae,was_provisional)
       VALUES (?,?,?,?,0.68,6,'WIN',1,1,1,0.1024,3,0)`,
    ).bind(predictionId, s.gameId, s.seasonId, s.modelVersion).run();

    const res = await get(`/teams/${await slugOf('架空ホームクラブ')}`, { token: null });
    expect(res.status).toBe(200);
    const team = (await res.json<{ data: {
      record: { wins: number; losses: number }; last5: string[]; elo: number | null;
      accuracy: { accuracy: number | null; n: number };
      history: { ownWinProb: number | null; isCorrect: boolean | null; isHome: boolean }[];
    } }>()).data;

    expect(team.record).toEqual({ wins: 1, losses: 0 });
    expect(team.last5).toEqual(['W']);
    expect(team.elo).toBeCloseTo(1582.5);
    // **母数を必ず返す**（要件 8.3）
    expect(team.accuracy.n).toBe(1);
    expect(team.history).toHaveLength(1);
    // ホーム側なので ownWinProb は home_win_prob のまま
    expect(team.history[0]!.ownWinProb).toBeCloseTo(0.68);
    expect(team.history[0]!.isHome).toBe(true);
    // **外れた試合を隠さない**（要件 8.3）。ここは的中
    expect(team.history[0]!.isCorrect).toBe(true);
  });

  it('アウェイ側の履歴は勝率を 1 - p に読み替える', async () => {
    const date = nextDate();
    const s = await seedGame({ tipoffAt: '2026-09-22T10:05:00Z', status: 'FINISHED' });
    await seedClubSeasons(s);
    await moveTo(s, date);
    await env.DB.prepare(
      `INSERT INTO team_games (game_id,club_id,opponent_id,season_id,game_date,is_home,
         competition,result,margin) VALUES (?,?,?,?,?,0,'REGULAR',0,-7)`,
    ).bind(s.gameId, s.awayId, s.homeId, s.seasonId, date).run();
    await seedPrediction(s, { isFinal: 1, homeWinProb: 0.68 });

    const team = (await (await get(`/teams/${await slugOf('架空アウェイクラブ')}`,
      { token: null })).json<{
      data: { history: { ownWinProb: number | null; won: boolean }[] };
    }>()).data;
    expect(team.history[0]!.ownWinProb).toBeCloseTo(0.32);
    expect(team.history[0]!.won).toBe(false);
  });

  it('的中率の母数が0なら率を返さない（分母0の率を作らない）', async () => {
    const s = await seedGame({ tipoffAt: '2026-09-22T10:05:00Z' });
    await seedClubSeasons(s);
    await moveTo(s, nextDate());
    const team = (await (await get(`/teams/${await slugOf('架空ホームクラブ')}`,
      { token: null })).json<{
      data: { accuracy: { accuracy: number | null; n: number } };
    }>()).data;
    expect(team.accuracy).toEqual({ accuracy: null, n: 0 });
  });
});

describe('公開エンドポイントはキャッシュ方針を必ず持つ（詳細設計 3.5）', () => {
  it.each([['/games?date='], ['/results?date='], ['/teams']])(
    '%s は Cache-Control を返す', async (prefix) => {
    const date = nextDate();
    const s = await seedGame({ tipoffAt: '2026-09-22T10:05:00Z' });
    await seedClubSeasons(s);
    await moveTo(s, date);
    const res = await get(prefix === '/teams' ? prefix : `${prefix}${date}`, { token: null });
    expect(res.status).toBe(200);
    // 指定を忘れるとキャッシュが効かず D1 に直撃する
    expect(res.headers.get('cache-control')).toMatch(/^public, max-age=60/);
  },
  );
});
