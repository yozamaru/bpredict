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

describe('GET /games?date=（一覧の実績の併記。v1.131）', () => {
  /** 一覧の1件を取る。**終了した試合の実績が出ることを確かめる。** */
  type ListBody = { data: { games: {
    homeScore: number | null; awayScore: number | null;
    evaluation: { isCorrect: boolean | null; scoreError: number | null } | null;
  }[] } };

  const first = async (date: string) => {
    const res = await get(`/games?date=${date}`, { token: null });
    expect(res.status).toBe(200);
    return (await res.json<ListBody>()).data.games[0]!;
  };

  it('終了した試合は実績のスコアと判定を返す', async () => {
    // 運営者の指摘「詳細画面じゃないと結果が分からない」（詳細設計 3.3 の v1.131）
    const date = nextDate();
    const s = await seedGame({ tipoffAt: '2026-09-22T10:05:00Z', status: 'FINISHED' });
    await seedClubSeasons(s);
    await moveTo(s, date);
    await env.DB.prepare('UPDATE games SET home_score = 81, away_score = 87 WHERE id = ?')
      .bind(s.gameId).run();
    const predictionId = await seedPrediction(s, { isFinal: 1 });
    await env.DB.prepare(
      `INSERT INTO prediction_results
         (prediction_id,game_id,season_id,model_version,home_win_prob,prob_bucket,outcome,
          predicted_home_win,actual_home_win,is_correct,brier,score_mae,was_provisional)
       VALUES (?,?,?,?,0.68,6,'LOSS',1,0,0,0.4624,5.5,0)`,
    ).bind(predictionId, s.gameId, s.seasonId, s.modelVersion).run();

    const game = await first(date);
    expect([game.homeScore, game.awayScore]).toEqual([81, 87]);
    expect(game.evaluation).toEqual({ isCorrect: false, scoreError: 5.5 });
  });

  it('未実施は、列に値があってもスコアを返さない', async () => {
    // **`status` が FINISHED でなければ出さない。** `/games/:gameId` と同じ関門
    const date = nextDate();
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    await seedClubSeasons(s);
    await moveTo(s, date);
    await env.DB.prepare('UPDATE games SET home_score = 40, away_score = 38 WHERE id = ?')
      .bind(s.gameId).run();
    await seedPrediction(s);

    const game = await first(date);
    expect([game.homeScore, game.awayScore]).toEqual([null, null]);
    expect(game.evaluation).toBeNull();
  });

  it('照合していなければ判定は null（false で埋めない）', async () => {
    // 終了してもすぐには付かない（freeze は毎時、照合は日次。基本設計 4.1）。
    // **null は「まだ照合していない」であって「外した」ではない**
    const date = nextDate();
    const s = await seedGame({ tipoffAt: '2026-09-22T10:05:00Z', status: 'FINISHED' });
    await seedClubSeasons(s);
    await moveTo(s, date);
    await env.DB.prepare('UPDATE games SET home_score = 81, away_score = 87 WHERE id = ?')
      .bind(s.gameId).run();
    await seedPrediction(s, { isFinal: 1 });

    const game = await first(date);
    expect([game.homeScore, game.awayScore]).toEqual([81, 87]);
    expect(game.evaluation).toBeNull();
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
  async function seedFinished(
    over: { isCorrect: number; outcome?: string; date: string; bucket?: number },
  ) {
    // **帯を変えられるようにする。** 50%未満の帯でしか出ない誤りがあり、
    // 6（60-70%）に固定したままでは検査が空振りする
    const bucket = over.bucket ?? 6;
    const prob = bucket / 10 + 0.05;
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
       VALUES (?,?,?,?,?,?,?,?,1,?,0.1024,3,0)`,
    ).bind(predictionId, s.gameId, s.seasonId, s.modelVersion, prob, bucket,
           over.outcome ?? 'WIN', prob > 0.5 ? 1 : 0, over.isCorrect).run();
    return s;
  }

  /**
   * 確率帯の通算成績。`bucketContext` の出どころ（詳細設計 1.6 / 3.3）。
   *
   * **`hit_rate`（的中率）と `actual_rate`（ホーム勝率）を別に受ける。**
   * 同じ値で埋めると、`actual_rate` を読んでいた実装でも通ってしまう。
   */
  async function seedBucket(
    key: string, n: number, hitRate: number | null, actualRate = 0.5,
  ) {
    await env.DB.prepare(
      `INSERT INTO accuracy_summary
         (scope,scope_key,model_version,n,accuracy,brier,actual_rate,hit_rate)
       VALUES ('BUCKET',?, '', ?, 0.65, 0.2, ?, ?)
       ON CONFLICT(scope,scope_key,model_version) DO UPDATE
          SET n = excluded.n, actual_rate = excluded.actual_rate,
              hit_rate = excluded.hit_rate`,
    ).bind(key, n, actualRate, hitRate).run();
  }

  type ResultsBody = { data: { results: { homeScore: number; evaluation: {
    isCorrect: boolean;
    bucketContext: { bucket: string; n: number; correct: number; rate: number } | null;
  } }[] } };

  it('的中した試合を実績と予測の対比で返す', async () => {
    const date = nextDate();
    await seedFinished({ isCorrect: 1, date });
    await seedBucket('60-70%', 42, 0.69);
    const body = await (await get(`/results?date=${date}`, { token: null }))
      .json<ResultsBody>();
    expect(body.data.results).toHaveLength(1);
    expect(body.data.results[0]!.homeScore).toBe(88);
    expect(body.data.results[0]!.evaluation.isCorrect).toBe(true);
    // 確率帯は「60-70%」の形（prob_bucket = 6）
    expect(body.data.results[0]!.evaluation.bucketContext?.bucket).toBe('60-70%');
  });

  it('**外れた試合も同じ形で返す**（隠さない。要件 8.3）', async () => {
    const date = nextDate();
    await seedFinished({ isCorrect: 0, date });
    const body = await (await get(`/results?date=${date}`, { token: null }))
      .json<ResultsBody>();
    expect(body.data.results).toHaveLength(1);
    expect(body.data.results[0]!.evaluation.isCorrect).toBe(false);
  });

  it('**外れた試合にその確率帯の通算的中率が付く**（要件 8.3）', async () => {
    // 帯のラベルだけでは「68%と予想した試合は42試合中29試合が的中」を出せない。
    // **母数（n）と的中数（correct）が要る**
    const date = nextDate();
    await seedFinished({ isCorrect: 0, date });
    await seedBucket('60-70%', 42, 0.69);
    const body = await (await get(`/results?date=${date}`, { token: null }))
      .json<ResultsBody>();
    const context = body.data.results[0]!.evaluation.bucketContext;
    expect(context).not.toBeNull();
    expect(context!.n).toBe(42);
    // correct は hit_rate × n から戻す（件数の列は持っていない）
    expect(context!.correct).toBe(29);
    expect(context!.rate).toBeCloseTo(0.69, 5);
  });

  it('**的中率は hit_rate から出す。`actual_rate` ではない**（詳細設計 1.6）', async () => {
    // 27% と予想してアウェイが勝つ帯 — **予測は当たっているがホーム勝率は0%**。
    // `actual_rate` を読んでいた実装は「0試合が的中（0.0%）」と出し、
    // **「予測どおりでした」のすぐ下に矛盾した数字を並べていた**
    // （2026-10-08 の本番で6試合中3試合。運営者の指摘で見つかった）
    const date = nextDate();
    await seedFinished({ isCorrect: 1, date, bucket: 2 });
    await seedBucket('20-30%', 4, 1.0, 0.0);
    const body = await (await get(`/results?date=${date}`, { token: null }))
      .json<ResultsBody>();
    const context = body.data.results[0]!.evaluation.bucketContext;
    expect(context!.bucket).toBe('20-30%');
    expect(context!.rate).toBeCloseTo(1.0, 5);
    expect(context!.correct).toBe(4);
  });

  it('**hit_rate が NULL の帯は返さない**（0 として出さない。詳細設計 3.3）', async () => {
    // 「0.0%が的中」は「1件も当たっていない」という意味を持ってしまう
    const date = nextDate();
    await seedFinished({ isCorrect: 1, date, bucket: 3 });
    await seedBucket('30-40%', 7, null, 0.4);
    const body = await (await get(`/results?date=${date}`, { token: null }))
      .json<ResultsBody>();
    expect(body.data.results[0]!.evaluation.bucketContext).toBeNull();
  });

  it('通算成績がまだ無ければ bucketContext は null（帯だけを出さない）', async () => {
    const date = nextDate();
    await seedFinished({ isCorrect: 1, date });
    await env.DB.prepare(
      `DELETE FROM accuracy_summary WHERE scope='BUCKET' AND scope_key='60-70%'`,
    ).run();
    const body = await (await get(`/results?date=${date}`, { token: null }))
      .json<ResultsBody>();
    expect(body.data.results[0]!.evaluation.bucketContext).toBeNull();
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
