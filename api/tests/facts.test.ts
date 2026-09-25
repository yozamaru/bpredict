import { env } from 'cloudflare:test';
import { beforeAll, beforeEach, describe, expect, it } from 'vitest';

import { applyMigrations, get, post, resetAll, seedGame } from './helpers';

beforeAll(async () => { await applyMigrations(); });
beforeEach(async () => { await resetAll(); });

const count = async (sql: string, ...bind: unknown[]) =>
  (await env.DB.prepare(sql).bind(...bind).first<{ n: number }>())?.n ?? -1;

describe('試合の取り込み', () => {
  it('公式試合IDを主キーに upsert する（延期で日付が変わっても別レコードにならない）', async () => {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    const base = {
      id: s.gameId, seasonId: s.seasonId, league: 'PREMIER', competition: 'REGULAR',
      gameDate: '2026-09-22', tipoffAt: '2099-01-01T10:05:00Z',
      homeClubId: s.homeId, awayClubId: s.awayId, status: 'SCHEDULED',
    };
    const first = await post('/internal/games', { games: [base] });
    expect(first.status).toBe(200);

    // 延期して日付が変わる
    const moved = { ...base, gameDate: '2026-09-25', status: 'POSTPONED' };
    const second = await post('/internal/games', { games: [moved] });
    expect(second.status).toBe(200);

    expect(await count('SELECT COUNT(*) AS n FROM games WHERE id = ?', s.gameId)).toBe(1);
    const row = await env.DB.prepare('SELECT game_date, status FROM games WHERE id = ?')
      .bind(s.gameId).first<{ game_date: string; status: string }>();
    expect(row).toEqual({ game_date: '2026-09-25', status: 'POSTPONED' });
  });

  it('取り込み対象外の大会区分は 400（CHECK の前に境界で弾く）', async () => {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    for (const competition of ['ALLSTAR', 'PRESEASON', 'regular', '']) {
      const res = await post('/internal/games', {
        games: [{
          id: 'x', seasonId: s.seasonId, league: 'PREMIER', competition,
          gameDate: '2026-09-22', tipoffAt: '2099-01-01T10:05:00Z',
          homeClubId: s.homeId, awayClubId: s.awayId, status: 'SCHEDULED',
        }],
      });
      expect(res.status).toBe(400);
    }
    expect(await count("SELECT COUNT(*) AS n FROM games WHERE id = 'x'")).toBe(0);
  });

  it('同一クラブ同士の試合は 400', async () => {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    const res = await post('/internal/games', {
      games: [{
        id: 'x', seasonId: s.seasonId, league: 'PREMIER', competition: 'REGULAR',
        gameDate: '2026-09-22', tipoffAt: '2099-01-01T10:05:00Z',
        homeClubId: s.homeId, awayClubId: s.homeId, status: 'SCHEDULED',
      }],
    });
    expect(res.status).toBe(400);
  });

  it('spectatorRestricted に NULL を入れられる（判定不能）', async () => {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    const res = await post('/internal/games', {
      games: [{
        id: s.gameId, seasonId: s.seasonId, league: 'PREMIER', competition: 'REGULAR',
        gameDate: '2026-09-22', tipoffAt: '2099-01-01T10:05:00Z',
        homeClubId: s.homeId, awayClubId: s.awayId, status: 'FINISHED',
        spectatorRestricted: null,
      }],
    });
    expect(res.status).toBe(200);
    const row = await env.DB.prepare('SELECT spectator_restricted AS v FROM games WHERE id = ?')
      .bind(s.gameId).first<{ v: number | null }>();
    expect(row?.v).toBeNull();
  });
});

describe('スタッツの取り込み', () => {
  it('恒等式に反する行は CHECK に当たり、1行も入らない（単一 batch）', async () => {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    const good = {
      gameId: s.gameId, playerId: s.playerId, clubId: s.homeId, gameDate: '2026-09-22',
      minutes: 31.2, fg2m: 4, fg2a: 8, fg3m: 2, fg3a: 5, ftm: 3, fta: 4,
      oreb: 1, dreb: 3, ast: 6, tov: 2, stl: 1, blk: 0, pf: 2, fd: 3,
      plusMinus: 5, pts: 20, fetchedAt: '2026-09-22T13:00:00Z',
    };
    // fg2m > fg2a は DDL の CHECK に反する
    const bad = { ...good, playerId: s.playerId, fg2m: 9, fg2a: 8 };
    const res = await post('/internal/stats', { playerGameStats: [good, bad] });
    expect(res.status).toBe(500);
    expect(await count('SELECT COUNT(*) AS n FROM player_game_stats WHERE game_id = ?', s.gameId)).toBe(0);
  });

  it('pf が 0-6 の外なら 400', async () => {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    const res = await post('/internal/stats', {
      playerGameStats: [{
        gameId: s.gameId, playerId: s.playerId, clubId: s.homeId, gameDate: '2026-09-22',
        pf: 7, fetchedAt: '2026-09-22T13:00:00Z',
      }],
    });
    expect(res.status).toBe(400);
  });
});

describe('エントリーの洗い替え（要件 5.5）', () => {
  it('取得ごとに全行を入れ替える（推定行が残らない）', async () => {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    const at = '2026-09-22T09:00:00Z';
    await post('/internal/entries', {
      gameId: s.gameId,
      entries: [{ gameId: s.gameId, playerId: s.playerId, status: 'ENTRY', source: 'ESTIMATED', fetchedAt: at }],
    });
    expect(await count('SELECT COUNT(*) AS n FROM game_entries WHERE source = ?', 'ESTIMATED')).toBe(1);

    // 公式が出たら推定行は消える
    const res = await post('/internal/entries', {
      gameId: s.gameId,
      entries: [{ gameId: s.gameId, playerId: s.playerId, status: 'OUT', source: 'OFFICIAL', fetchedAt: at }],
    });
    expect(res.status).toBe(200);
    expect(await count('SELECT COUNT(*) AS n FROM game_entries WHERE source = ?', 'ESTIMATED')).toBe(0);
    expect(await count('SELECT COUNT(*) AS n FROM game_entries WHERE source = ?', 'OFFICIAL')).toBe(1);
  });

  it('空配列で全消しできる', async () => {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    await post('/internal/entries', {
      gameId: s.gameId,
      entries: [{ gameId: s.gameId, playerId: s.playerId, status: 'ENTRY', source: 'OFFICIAL', fetchedAt: '2026-09-22T09:00:00Z' }],
    });
    await post('/internal/entries', { gameId: s.gameId, entries: [] });
    expect(await count('SELECT COUNT(*) AS n FROM game_entries WHERE game_id = ?', s.gameId)).toBe(0);
  });

  it('本文の gameId と食い違う entries は 400', async () => {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    const res = await post('/internal/entries', {
      gameId: s.gameId,
      entries: [{ gameId: 'other', playerId: s.playerId, status: 'ENTRY', source: 'OFFICIAL', fetchedAt: '2026-09-22T09:00:00Z' }],
    });
    expect(res.status).toBe(400);
  });
});

describe('レーティングの洗い替え', () => {
  it('対象期間を DELETE してから INSERT する（差分更新しない）', async () => {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    const r = (asOfDate: string, elo: number) => ({
      clubId: s.homeId, asOfDate, seasonId: s.seasonId, elo, gamesPlayed: 10,
    });
    await post('/internal/ratings', {
      fromDate: '2026-09-01', toDate: '2026-09-30',
      ratings: [r('2026-09-10', 1500), r('2026-09-20', 1520)],
    });
    expect(await count('SELECT COUNT(*) AS n FROM team_ratings')).toBe(2);

    // 同じ期間を1行で洗い替える
    const res = await post('/internal/ratings', {
      fromDate: '2026-09-01', toDate: '2026-09-30', ratings: [r('2026-09-10', 1480)],
    });
    expect(res.status).toBe(200);
    expect(await count('SELECT COUNT(*) AS n FROM team_ratings')).toBe(1);
    const row = await env.DB.prepare('SELECT elo FROM team_ratings').first<{ elo: number }>();
    expect(row?.elo).toBe(1480);
  });

  it('期間外の asOfDate が混ざっていたら 400', async () => {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    const res = await post('/internal/ratings', {
      fromDate: '2026-09-01', toDate: '2026-09-30',
      ratings: [{ clubId: s.homeId, asOfDate: '2026-10-05', seasonId: s.seasonId, elo: 1500, gamesPlayed: 1 }],
    });
    expect(res.status).toBe(400);
  });
});

describe('会場の履歴の洗い替え', () => {
  /** 会場を1つ作る。`venue_revisions` は FK で `venues` を参照する。 */
  async function seedVenue(id: string, name = '架空アリーナ') {
    await env.DB.prepare('INSERT INTO venues (id,name) VALUES (?,?)').bind(id, name).run();
    return id;
  }

  it('対象期間を DELETE してから INSERT する（差分更新しない）', async () => {
    const venueId = await seedVenue('v-rev-1');
    const rev = (validFrom: string, validTo: string, name: string, capacity: number | null) => ({
      venueId, validFrom, validTo, name, capacity,
    });
    await post('/internal/venue-revisions', {
      fromDate: '2016-09-22', toDate: '9999-12-31',
      revisions: [
        rev('2016-09-22', '2019-06-30', '旧名アリーナ', 5000),
        rev('2019-07-01', '9999-12-31', '新名アリーナ', 5200),
      ],
    });
    expect(await count('SELECT COUNT(*) AS n FROM venue_revisions')).toBe(2);

    // 同じ期間を1行で洗い替える
    const res = await post('/internal/venue-revisions', {
      fromDate: '2016-09-22', toDate: '9999-12-31',
      revisions: [rev('2016-09-22', '9999-12-31', '統合名アリーナ', 5100)],
    });
    expect(res.status).toBe(200);
    expect(await count('SELECT COUNT(*) AS n FROM venue_revisions')).toBe(1);
    const row = await env.DB.prepare('SELECT name, capacity FROM venue_revisions')
      .first<{ name: string; capacity: number }>();
    expect(row?.name).toBe('統合名アリーナ');
    expect(row?.capacity).toBe(5100);
  });

  it('capacity は NULL を受ける（手入力 CSV に行がない会場）', async () => {
    const venueId = await seedVenue('v-rev-2');
    const res = await post('/internal/venue-revisions', {
      fromDate: '2016-09-22', toDate: '9999-12-31',
      revisions: [{ venueId, validFrom: '2016-09-22', validTo: '9999-12-31',
                    name: '架空アリーナ', capacity: null }],
    });
    expect(res.status).toBe(200);
    const row = await env.DB.prepare('SELECT capacity FROM venue_revisions')
      .first<{ capacity: number | null }>();
    expect(row?.capacity).toBeNull();
  });

  it('期間外の validFrom が混ざっていたら 400', async () => {
    const venueId = await seedVenue('v-rev-3');
    const res = await post('/internal/venue-revisions', {
      fromDate: '2016-09-22', toDate: '2020-12-31',
      revisions: [{ venueId, validFrom: '2021-01-01', validTo: '9999-12-31',
                    name: '架空アリーナ', capacity: null }],
    });
    expect(res.status).toBe(400);
  });

  it('validFrom が validTo より後なら 400', async () => {
    const venueId = await seedVenue('v-rev-4');
    const res = await post('/internal/venue-revisions', {
      fromDate: '2016-09-22', toDate: '9999-12-31',
      revisions: [{ venueId, validFrom: '2020-01-01', validTo: '2019-01-01',
                    name: '架空アリーナ', capacity: null }],
    });
    expect(res.status).toBe(400);
  });

  it('(venueId, validFrom) が重複していたら 400（主キー違反の前に弾く）', async () => {
    const venueId = await seedVenue('v-rev-5');
    const res = await post('/internal/venue-revisions', {
      fromDate: '2016-09-22', toDate: '9999-12-31',
      revisions: [
        { venueId, validFrom: '2016-09-22', validTo: '2019-06-30', name: 'A', capacity: null },
        { venueId, validFrom: '2016-09-22', validTo: '9999-12-31', name: 'B', capacity: null },
      ],
    });
    expect(res.status).toBe(400);
  });

  it('未知のキーを受け付けない（汎用の書き込み口にしない）', async () => {
    const venueId = await seedVenue('v-rev-6');
    const res = await post('/internal/venue-revisions', {
      fromDate: '2016-09-22', toDate: '9999-12-31',
      revisions: [{ venueId, validFrom: '2016-09-22', validTo: '9999-12-31',
                    name: 'A', capacity: null, table: 'clubs' }],
    });
    expect(res.status).toBe(400);
  });
});

describe('backfill の再開判定', () => {
  /** 当該試合のチームスタッツを入れる（= 取り込みが完了した状態にする）。 */
  async function seedTeamStats(gameId: string, clubIds: string[]) {
    await env.DB.batch(clubIds.map((clubId, index) => env.DB.prepare(
      `INSERT INTO team_game_stats (game_id, club_id, game_date, is_home, pts, fetched_at)
       VALUES (?,?,?,?,?,?)`,
    ).bind(gameId, clubId, '2026-09-22', index === 0 ? 1 : 0, 80, '2026-09-22T12:00:00Z')));
  }

  async function ingestedIds(seasonId: string): Promise<string[]> {
    const res = await get(`/internal/games/ingested?seasonId=${seasonId}`);
    expect(res.status).toBe(200);
    const body = await res.json<{ data: { count: number; gameIds: string[] } }>();
    return body.data.gameIds;
  }

  it('スタッツまで入った試合を取り込み済みとして返す', async () => {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    await seedTeamStats(s.gameId, [s.homeId, s.awayId]);
    expect(await ingestedIds(s.seasonId)).toEqual([s.gameId]);
  });

  it('試合行だけでスタッツがない試合を「済み」と見なさない', async () => {
    // **これが穴だった。** backfill は1試合につき games と stats を続けて投げる。
    // その間で失敗すると（書き込み枠の枯渇、一時的な 5xx）試合行だけが残る。
    // 試合行の有無で判定すると、再開時にその試合が飛ばされ、**スタッツが
    // 永久に欠ける**。しかも件数にも `不正` にも出ないので気づけない。
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    expect(await ingestedIds(s.seasonId)).toEqual([]);
  });

  it('スタッツが入った時点で取り込み済みに変わる', async () => {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    expect(await ingestedIds(s.seasonId)).toEqual([]);
    await seedTeamStats(s.gameId, [s.homeId, s.awayId]);
    expect(await ingestedIds(s.seasonId)).toEqual([s.gameId]);
  });

  it('seasonId がなければ 400', async () => {
    expect((await get('/internal/games/ingested')).status).toBe(400);
  });

  it('Bearer なしは 401', async () => {
    expect((await get('/internal/games/ingested?seasonId=x', { token: null })).status).toBe(401);
  });
});

describe('POST /internal/games（試合データから導かれるマスタ）', () => {
  // backfill は会場・選手・年度断面を同じ試合レスポンスから作る（詳細設計 3.4）。
  // FK の順序で同一 batch() に入るため、1リクエストで完結する。
  it('会場・選手・club_seasons を試合と同じ本文で受け、FK 順で入る', async () => {
    await resetAll();
    const seed = await seedGame({ tipoffAt: '2026-09-22T10:05:00Z', status: 'SCHEDULED' });
    const body = {
      venues: [{ id: 'v-new', name: '架空アリーナ', prefecture: '東京都' }],
      venueSourceKeys: [{ sourceCode: 'v-new', venueId: 'v-new' }],
      players: [{ id: 'p-new', name: '架空 選手', heightCm: 190 }],
      clubSeasons: [
        {
          clubId: seed.homeId,
          seasonId: seed.seasonId,
          name: '架空クラブ',
          shortName: '架空',
          league: 'B1',
          primaryVenueId: 'v-new',
        },
      ],
      games: [
        {
          id: `${seed.gameId}-m`,
          seasonId: seed.seasonId,
          league: 'B1',
          competition: 'REGULAR',
          // シードの試合と同一カード・同一日にすると
          // UNIQUE (season_id, game_date, home_club_id, away_club_id) に当たる
          gameDate: '2026-09-23',
          tipoffAt: '2026-09-23T10:05:00Z',
          homeClubId: seed.homeId,
          awayClubId: seed.awayId,
          venueId: 'v-new',
          status: 'SCHEDULED',
        },
      ],
    };
    const first = await post('/internal/games', body);
    expect(first.status).toBe(200);
    const payload = await first.json<{ data: { applied: Record<string, number> } }>();
    expect(payload.data.applied).toMatchObject({
      venues: 1,
      venueSourceKeys: 1,
      players: 1,
      clubSeasons: 1,
      games: 1,
    });

    // 会場が先に入っているため、試合の FK が成立する
    const venue = await env.DB.prepare('SELECT name FROM venues WHERE id = ?').bind('v-new').first();
    expect(venue).toMatchObject({ name: '架空アリーナ' });
    const game = await env.DB.prepare('SELECT venue_id FROM games WHERE id = ?')
      .bind(`${seed.gameId}-m`)
      .first();
    expect(game).toMatchObject({ venue_id: 'v-new' });

    // 2回流しても増えない（冪等）
    expect((await post('/internal/games', body)).status).toBe(200);
    const counts = await env.DB.prepare(
      'SELECT (SELECT COUNT(*) FROM venues WHERE id = ?) AS v,' +
        ' (SELECT COUNT(*) FROM players WHERE id = ?) AS p',
    )
      .bind('v-new', 'p-new')
      .first<{ v: number; p: number }>();
    expect(counts).toMatchObject({ v: 1, p: 1 });
  });

  it('会場の名称は上書きしない（当時の名称で現在の表示名を壊さない）', async () => {
    await resetAll();
    const seed = await seedGame({ tipoffAt: '2026-09-22T10:05:00Z', status: 'SCHEDULED' });
    const base = {
      games: [
        {
          id: `${seed.gameId}-n`,
          seasonId: seed.seasonId,
          league: 'B1',
          competition: 'REGULAR',
          gameDate: '2026-09-24',
          tipoffAt: '2026-09-24T10:05:00Z',
          homeClubId: seed.homeId,
          awayClubId: seed.awayId,
          venueId: 'v-keep',
          status: 'SCHEDULED',
        },
      ],
    };
    await post('/internal/games', {
      ...base,
      venues: [{ id: 'v-keep', name: '現在の名称', prefecture: '東京都' }],
    });
    await post('/internal/games', {
      ...base,
      venues: [{ id: 'v-keep', name: '当時の名称', prefecture: '大阪府' }],
    });
    const venue = await env.DB.prepare('SELECT name, prefecture FROM venues WHERE id = ?')
      .bind('v-keep')
      .first();
    expect(venue).toMatchObject({ name: '現在の名称', prefecture: '大阪府' });
  });

  it('知らない配列名を受け付けない', async () => {
    const response = await post('/internal/games', {
      games: [],
      venueRevisions: [{ venueId: 'v', validFrom: '2016-09-01', name: 'x' }],
    });
    expect(response.status).toBe(400);
  });
});

describe('座標を解決する会場の一覧（詳細設計 3.4 / 4.10）', () => {
  /** 会場を1件入れる。座標は任意（未解決を表すため）。 */
  async function seedVenue(
    id: string, name: string, coords: { lat: number; lng: number } | null = null,
  ) {
    await env.DB.prepare(
      'INSERT INTO venues (id, name, prefecture, lat, lng) VALUES (?,?,?,?,?)',
    ).bind(id, name, coords ? '架空県' : null, coords?.lat ?? null, coords?.lng ?? null).run();
  }

  async function venues(query = ''): Promise<{ id: string; lat: number | null }[]> {
    const res = await get(`/internal/venues${query}`);
    expect(res.status).toBe(200);
    const body = await res.json<{
      data: { count: number; venues: { id: string; lat: number | null }[] };
    }>();
    expect(body.data.count).toBe(body.data.venues.length);
    return body.data.venues;
  }

  it('既定は全件を返す（IDの昇順）', async () => {
    await seedVenue('v2', '架空アリーナ2', { lat: 35.1, lng: 139.1 });
    await seedVenue('v1', '架空アリーナ1');
    expect((await venues()).map((v) => v.id)).toEqual(['v1', 'v2']);
  });

  it('missingCoordinates=1 は座標が未解決の会場だけを返す', async () => {
    await seedVenue('v1', '架空アリーナ1');
    await seedVenue('v2', '架空アリーナ2', { lat: 35.1, lng: 139.1 });
    const rows = await venues('?missingCoordinates=1');
    expect(rows.map((v) => v.id)).toEqual(['v1']);
    expect(rows[0]?.lat).toBeNull();
  });

  it('片方だけ入っている会場も未解決として返す', async () => {
    // 緯度だけ入って経度が NULL の行は座標として使えない。
    // `lat IS NULL AND lng IS NULL` で絞ると、この行が永久に解決されない
    await env.DB.prepare('INSERT INTO venues (id, name, lat) VALUES (?,?,?)')
      .bind('v3', '架空アリーナ3', 35.1).run();
    expect((await venues('?missingCoordinates=1')).map((v) => v.id)).toEqual(['v3']);
  });

  it('会場が1件もなくても 200 で空を返す（エラーにしない）', async () => {
    expect(await venues()).toEqual([]);
  });

  it('missingCoordinates に 1 以外を渡したら 400', async () => {
    // 値を黙って無視すると「絞ったつもりで全件が返る」ことに気づけない
    for (const value of ['0', 'true', 'yes', '']) {
      const res = await get(`/internal/venues?missingCoordinates=${value}`);
      expect(res.status).toBe(400);
    }
  });

  it('Bearer なしでは 401', async () => {
    const res = await get('/internal/venues', { token: null });
    expect(res.status).toBe(401);
  });
});
