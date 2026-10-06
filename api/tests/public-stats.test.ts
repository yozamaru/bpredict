/**
 * 戦績・スタッツの閲覧（詳細設計 1.9 / 3.3 / 3.4。要件 F-10 / F-15 / A-19）。
 *
 * **出すのは集計値だけである**（要件 3.1.1）。試合ごとの記録の一覧を返さない。
 */
import { env } from 'cloudflare:test';
import { beforeEach, describe, expect, it } from 'vitest';

import { applyMigrations, get, post, resetAll, seedGame } from './helpers';

// **テストごとに消す。** この表は主キーがキー集合であり、前のテストの行が残ると
// 件数の検査（upsert が洗い替えでないことの確認）が成立しない
beforeEach(async () => {
  await applyMigrations();
  await resetAll();
});

async function body(res: Response): Promise<Record<string, unknown>> {
  const json: { data?: Record<string, unknown> } = await res.json();
  expect(json.data).toBeTruthy();
  return json.data ?? {};
}

const COUNTS = {
  fg2m: 49, fg2a: 94, fg3m: 27, fg3a: 64, ftm: 39, fta: 47,
  oreb: 7, dreb: 34, ast: 74, tov: 26, stl: 13, blk: 4, pf: 29, fd: 37,
};

function playerRow(over: Record<string, unknown> = {}) {
  return {
    playerId: 'px', scope: 'SEASON', scopeKey: 'sx', clubId: 'cx',
    games: 12, gamesStarted: 10, minutes: 361.2, pts: 212, ...COUNTS, ...over,
  };
}

function teamRow(over: Record<string, unknown> = {}) {
  return {
    clubId: 'cx', scope: 'SEASON', scopeKey: 'sx',
    games: 20, wins: 14, pointsFor: 1648, pointsAgainst: 1524, statGames: 20,
    ...COUNTS, ...over,
  };
}

describe('POST /internal/stat-summary', () => {
  it('1行も書かないリクエストを拒否する', async () => {
    const res = await post('/internal/stat-summary', {});
    expect(res.status).toBe(400);
    expect((await post('/internal/stat-summary', { playerStats: [], teamStats: [] })).status)
      .toBe(400);
  });

  it('CAREER の行に季やクラブを入れた本文を拒否する', async () => {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    for (const over of [
      { scope: 'CAREER', scopeKey: s.seasonId, clubId: '' },
      { scope: 'CAREER', scopeKey: '', clubId: s.homeId },
      { scope: 'SEASON', scopeKey: '', clubId: s.homeId },
      { scope: 'SEASON', scopeKey: s.seasonId, clubId: '' },
    ]) {
      const res = await post('/internal/stat-summary', {
        playerStats: [playerRow({ playerId: s.playerId, ...over })],
      });
      expect(res.status, JSON.stringify(over)).toBe(400);
    }
  });

  it('先発数が試合数を超える本文を拒否する', async () => {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    const res = await post('/internal/stat-summary', {
      playerStats: [playerRow({
        playerId: s.playerId, scopeKey: s.seasonId, clubId: s.homeId,
        games: 5, gamesStarted: 10,
      })],
    });
    expect(res.status).toBe(400);
  });

  it('成功数が試投数を超える本文を拒否する', async () => {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    const res = await post('/internal/stat-summary', {
      playerStats: [playerRow({
        playerId: s.playerId, scopeKey: s.seasonId, clubId: s.homeId,
        fg3m: 99, fg3a: 1,
      })],
    });
    expect(res.status).toBe(400);
  });

  it('同じキーを2回送っても1行のまま（upsert。洗い替えない）', async () => {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    const row = teamRow({ clubId: s.homeId, scopeKey: s.seasonId });
    expect((await post('/internal/stat-summary', { teamStats: [row] })).status).toBe(200);
    expect((await post('/internal/stat-summary', {
      teamStats: [{ ...row, wins: 15 }],
    })).status).toBe(200);
    const after = await env.DB
      .prepare('SELECT COUNT(*) AS n, MAX(wins) AS wins FROM team_stat_summary')
      .first<{ n: number; wins: number }>();
    expect(after?.n).toBe(1);
    expect(after?.wins).toBe(15);
  });

  it('別のキーを送っても前のキーが消えない（分割送信で成立する前提）', async () => {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    await post('/internal/stat-summary', {
      teamStats: [teamRow({ clubId: s.homeId, scopeKey: s.seasonId })],
    });
    await post('/internal/stat-summary', {
      teamStats: [teamRow({ clubId: s.awayId, scopeKey: s.seasonId })],
    });
    const n = await env.DB.prepare('SELECT COUNT(*) AS n FROM team_stat_summary')
      .first<{ n: number }>();
    expect(n?.n).toBe(2);
  });

  it('行数の上限を超えたら 400（テーブルごとに検査する）', async () => {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    const rows = Array.from({ length: 161 }, (_, i) =>
      playerRow({ playerId: s.playerId, scopeKey: `${s.seasonId}-${i}`, clubId: s.homeId }));
    const res = await post('/internal/stat-summary', { playerStats: rows });
    expect(res.status).toBe(400);
  });
});

describe('GET /players/:playerId', () => {
  it('形式が不正なら D1 に触らず 400', async () => {
    expect((await get('/players/' + encodeURIComponent('../etc/passwd'))).status).toBe(400);
    expect((await get('/players/abc')).status).toBe(400);
  });

  it('存在しない選手は 404', async () => {
    expect((await get('/players/999999')).status).toBe(404);
  });

  it('集計が1行もなければ 404（静的生成の範囲内でも空になる）', async () => {
    await env.DB.prepare("INSERT INTO players (id,name) VALUES ('12345','架空 選手')").run();
    expect((await get('/players/12345')).status).toBe(404);
  });

  it('通算と季を返し、1試合平均を導出する', async () => {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    await env.DB.prepare("INSERT INTO players (id,name) VALUES ('12345','架空 選手')").run();
    await post('/internal/stat-summary', {
      playerStats: [
        playerRow({ playerId: '12345', scopeKey: s.seasonId, clubId: s.homeId }),
        playerRow({
          playerId: '12345', scope: 'CAREER', scopeKey: '', clubId: '',
          games: 24, minutes: 722.4, pts: 424,
        }),
      ],
    });
    const data = await body(await get('/players/12345'));
    expect(Object.keys(data).sort()).toEqual(['career', 'current', 'player', 'seasons']);

    const career = data.career as Record<string, unknown>;
    expect(career.games).toBe(24);
    // 722.4 / 24 = 30.1
    expect((career.perGame as Record<string, number>).minutes).toBeCloseTo(30.1, 5);
    // 424 / 24 = 17.666... → 17.7
    expect((career.perGame as Record<string, number>).pts).toBeCloseTo(17.7, 5);

    const seasons = data.seasons as Record<string, unknown>[];
    expect(seasons).toHaveLength(1);
    expect(seasons[0]?.seasonId).toBe(s.seasonId);
  });

  it('率は合計を合計で割る（1試合ごとの率を平均しない）', async () => {
    await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    await env.DB.prepare("INSERT INTO players (id,name) VALUES ('12345','架空 選手')").run();
    await post('/internal/stat-summary', {
      playerStats: [playerRow({
        playerId: '12345', scope: 'CAREER', scopeKey: '', clubId: '',
      })],
    });
    const career = (await body(await get('/players/12345'))).career as Record<string, unknown>;
    const box = career.box as Record<string, { m: number; a: number; pct: number | null }>;
    // FG は 2P と 3P の合計として導出する（要件 6.8.2）
    expect(box.fg?.pct).toBeCloseTo((49 + 27) / (94 + 64), 3);
    expect(box.fg2?.pct).toBeCloseTo(49 / 94, 3);
  });

  it('試投数が0のときだけ率を出さない（実績に閾値を設けない）', async () => {
    await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    await env.DB.prepare("INSERT INTO players (id,name) VALUES ('12345','架空 選手')").run();
    await post('/internal/stat-summary', {
      playerStats: [playerRow({
        playerId: '12345', scope: 'CAREER', scopeKey: '', clubId: '',
        // 1本だけ投げて1本決めた → **閾値未満でも率を出す**
        fg3m: 1, fg3a: 1, ftm: 0, fta: 0,
      })],
    });
    const career = (await body(await get('/players/12345'))).career as Record<string, unknown>;
    const box = career.box as Record<string, { pct: number | null }>;
    expect(box.fg3?.pct).toBe(1);
    expect(box.ft?.pct).toBeNull();
  });

  it('季中に移籍した季は2行で返る（クラブ別に持つため）', async () => {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    await env.DB.prepare("INSERT INTO players (id,name) VALUES ('12345','架空 選手')").run();
    await post('/internal/stat-summary', {
      playerStats: [
        playerRow({
          playerId: '12345', scopeKey: s.seasonId, clubId: s.homeId,
          games: 5, gamesStarted: 4,
        }),
        playerRow({
          playerId: '12345', scopeKey: s.seasonId, clubId: s.awayId,
          games: 7, gamesStarted: 6,
        }),
      ],
    });
    const seasons = (await body(await get('/players/12345'))).seasons as Record<string, unknown>[];
    expect(seasons).toHaveLength(2);
    expect(seasons.map((r) => r.seasonId)).toEqual([s.seasonId, s.seasonId]);
    expect(new Set(seasons.map((r) => (r.club as Record<string, string>).slug)).size).toBe(2);
  });

  it('予測値を1つも返さない（この口が返すのはすべて実績である）', async () => {
    await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    await env.DB.prepare("INSERT INTO players (id,name) VALUES ('12345','架空 選手')").run();
    await post('/internal/stat-summary', {
      playerStats: [playerRow({
        playerId: '12345', scope: 'CAREER', scopeKey: '', clubId: '',
      })],
    });
    const text = await (await get('/players/12345')).text();
    expect(text).not.toContain('availProb');
    expect(text).not.toContain('predMinutes');
    expect(text).not.toContain('homeWinProb');
  });
});

describe('GET /teams/:slug の戦績とスタッツ', () => {
  it('シーズン別・通算・選手一覧を返す', async () => {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    await env.DB.batch([
      env.DB.prepare(
        `INSERT INTO club_seasons (club_id,season_id,name,short_name,league)
         VALUES (?,?,?,?,'PREMIER')`,
      ).bind(s.homeId, s.seasonId, '架空ホーム', '架空H'),
      env.DB.prepare(
        "INSERT INTO player_seasons (player_id,season_id,club_id,number,position) VALUES (?,?,?,'25','PF')",
      ).bind(s.playerId, s.seasonId, s.homeId),
    ]);
    await post('/internal/stat-summary', {
      teamStats: [
        teamRow({ clubId: s.homeId, scopeKey: s.seasonId }),
        teamRow({
          clubId: s.homeId, scope: 'CAREER', scopeKey: '',
          games: 613, wins: 342, pointsFor: 50512, pointsAgainst: 48803, statGames: 611,
        }),
      ],
      playerStats: [playerRow({
        playerId: s.playerId, scopeKey: s.seasonId, clubId: s.homeId,
      })],
    });

    const data = await body(await get(`/teams/home-${s.homeId.split('-')[1]}`));
    const seasons = data.seasons as Record<string, unknown>[];
    expect(seasons).toHaveLength(1);
    expect(seasons[0]?.wins).toBe(14);
    // **`losses` は返す**（列としては持たないが、画面に出る値である）
    expect(seasons[0]?.losses).toBe(6);

    const career = data.career as Record<string, unknown>;
    expect(career.games).toBe(613);
    expect(career.losses).toBe(271);
    // **母数が2つある。** ボックススコアは stat_games で割る
    expect((career.box as Record<string, unknown>).statGames).toBe(611);

    const roster = data.roster as Record<string, unknown>[];
    expect(roster).toHaveLength(1);
    expect(roster[0]?.playerId).toBe(s.playerId);
    expect(roster[0]?.number).toBe('25');
    // **主要4項目だけを返す**（段階開示。要件 8.3）
    expect(Object.keys(roster[0] ?? {}).sort())
      .toEqual(['games', 'name', 'number', 'perGame', 'playerId', 'position']);
  });

  it('集計がなくてもキーの位置が変わらない', async () => {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    const data = await body(await get(`/teams/home-${s.homeId.split('-')[1]}`));
    expect(data.seasons).toEqual([]);
    expect(data.career).toBeNull();
    expect(data.roster).toEqual([]);
  });
});
