import { createExecutionContext, env } from 'cloudflare:test';
import { beforeAll, beforeEach, describe, expect, it } from 'vitest';

import { applyMigrations, post, resetAll, TOKEN } from './helpers';

const season = {
  id: '2026-27-PREMIER',
  label: '2026-27',
  league: 'PREMIER',
  startDate: '2026-09-22',
  endDate: '2027-05-02',
};
const club = { id: '703', slug: 'utsunomiya-brex', name: '宇都宮ブレックス' };
const sourceId = {
  sourceId: '703',
  clubId: '703',
  validFrom: '2016-09-22',
  validTo: '9999-12-31',
  note: null,
};

beforeAll(async () => {
  await applyMigrations();
});

beforeEach(async () => {
  await resetAll();
});

describe('認証（詳細設計 7.2）', () => {
  it('Authorization がなければ 401', async () => {
    const res = await post('/internal/masters', { clubs: [club] }, { token: null });
    expect(res.status).toBe(401);
    expect(await res.json()).toMatchObject({ error: { code: 'UNAUTHORIZED' } });
  });

  it('別のトークンでは 401', async () => {
    const res = await post('/internal/masters', { clubs: [club] }, { token: 'wrong' });
    expect(res.status).toBe(401);
  });

  it('Bearer スキームがなければ 401', async () => {
    // `.replace("Bearer ", "")` はスキーム検証にならない。正規表現で厳格に見る
    const req = new Request('https://example.invalid/internal/masters', {
      method: 'POST',
      headers: { Authorization: TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ clubs: [club] }),
    });
    const worker = (await import('../src/index')).default;
    const res = await worker.fetch(
      req,
      { ...env, INGEST_TOKEN: TOKEN },
      createExecutionContext(),
    );
    expect(res.status).toBe(401);
  });

  it('INGEST_TOKEN_NEXT でも通る（2キー方式）', async () => {
    const res = await post(
      '/internal/masters',
      { clubs: [club] },
      { token: 'next-token', envOverride: { INGEST_TOKEN_NEXT: 'next-token' } },
    );
    expect(res.status).toBe(200);
  });
});

describe('入力検証（詳細設計 3.2）', () => {
  it('テーブル名を引数に取る形は 400（汎用エンドポイントにしない）', async () => {
    const res = await post('/internal/masters', { table: 'games', rows: [{ id: 'g1' }] });
    expect(res.status).toBe(400);
    expect(await res.json()).toMatchObject({ error: { code: 'BAD_REQUEST' } });
  });

  it('知らない配列名は 400', async () => {
    const res = await post('/internal/masters', { players: [{ id: 'p1', name: 'x' }] });
    expect(res.status).toBe(400);
  });

  it('3つとも無ければ 400', async () => {
    const res = await post('/internal/masters', {});
    expect(res.status).toBe(400);
  });

  it('league が CHECK 制約の集合外なら 400', async () => {
    const res = await post('/internal/masters', { seasons: [{ ...season, league: 'X' }] });
    expect(res.status).toBe(400);
  });

  it('日付が YYYY-MM-DD でなければ 400', async () => {
    const res = await post('/internal/masters', { seasons: [{ ...season, startDate: '2026/09/22' }] });
    expect(res.status).toBe(400);
  });

  it('startDate > endDate なら 400', async () => {
    const res = await post('/internal/masters', {
      seasons: [{ ...season, startDate: '2027-05-02', endDate: '2026-09-22' }],
    });
    expect(res.status).toBe(400);
  });

  it.each(['Utsunomiya_Brex', 'UTSUNOMIYA', 'ブレックス', ''])(
    'slug が形式に合わなければ 400 (%s)',
    async (slug) => {
      const res = await post('/internal/masters', { clubs: [{ ...club, slug }] });
      expect(res.status).toBe(400);
    },
  );

  it('JSON でなければ 400', async () => {
    const res = await post('/internal/masters', 'not json');
    expect(res.status).toBe(400);
  });

  it('行数が上限を超えたら 400（D1 を触らない）', async () => {
    // clubs は5列 → floor(100/5)*40 = 800
    const rows = Array.from({ length: 801 }, (_, i) => ({
      id: `c${i}`,
      slug: `club-${i}`,
      name: `架空${i}`,
    }));
    const res = await post('/internal/masters', { clubs: rows });
    expect(res.status).toBe(400);
    const n = await env.DB.prepare('SELECT COUNT(*) AS n FROM clubs').first<{ n: number }>();
    expect(n?.n).toBe(0);
  });
});

describe('投入（詳細設計 3.4）', () => {
  it('3種類をまとめて入れられる', async () => {
    const res = await post('/internal/masters', {
      seasons: [season],
      clubs: [club],
      clubSourceIds: [sourceId],
    });
    expect(res.status).toBe(200);
    expect(res.headers.get('Cache-Control')).toBe('no-store');
    const body = await res.json<{ data: { applied: Record<string, number>; statements: number } }>();
    expect(body.data.applied).toEqual({ seasons: 1, clubs: 1, clubSourceIds: 1 });
    expect(body.data.statements).toBe(3);

    const row = await env.DB.prepare(
      `SELECT c.id, c.slug, c.name FROM club_source_ids k
         JOIN clubs c ON c.id = k.club_id WHERE k.source_id = '703'`,
    ).first<{ id: string; slug: string; name: string }>();
    expect(row).toEqual(club);
  });

  it('冪等（2回流しても行数が増えず、値が更新される）', async () => {
    await post('/internal/masters', { seasons: [season], clubs: [club], clubSourceIds: [sourceId] });
    const res = await post('/internal/masters', {
      clubs: [{ ...club, name: '宇都宮ブレックス（改）' }],
    });
    expect(res.status).toBe(200);
    const n = await env.DB.prepare('SELECT COUNT(*) AS n FROM clubs').first<{ n: number }>();
    expect(n?.n).toBe(1);
    const row = await env.DB.prepare("SELECT name FROM clubs WHERE id='703'").first<{ name: string }>();
    expect(row?.name).toBe('宇都宮ブレックス（改）');
  });

  it('工程2の実データ規模（11 / 30 / 30）が1リクエスト5文で入る', async () => {
    const seasons = Array.from({ length: 11 }, (_, i) => ({
      ...season,
      id: `20${16 + i}-${17 + i}-B1`,
      label: `20${16 + i}-${17 + i}`,
      league: 'B1',
      startDate: `20${16 + i}-10-01`,
      endDate: `20${17 + i}-05-01`,
    }));
    const clubs = Array.from({ length: 30 }, (_, i) => ({
      id: `c${i}`,
      slug: `club-${i}`,
      name: `架空クラブ${i}`,
    }));
    const clubSourceIds = clubs.map((x) => ({
      sourceId: x.id,
      clubId: x.id,
      validFrom: '2016-09-22',
      validTo: '9999-12-31',
      note: null,
    }));
    const res = await post('/internal/masters', { seasons, clubs, clubSourceIds });
    expect(res.status).toBe(200);
    const body = await res.json<{ data: { statements: number } }>();
    // seasons 1文 + clubs 2文 + club_source_ids 2文 = 5文（詳細設計 3.4）
    expect(body.data.statements).toBe(5);
    const n = await env.DB.prepare('SELECT COUNT(*) AS n FROM club_source_ids').first<{ n: number }>();
    expect(n?.n).toBe(30);
  });

  it('clubs に無い club_id を参照したら失敗し、1行も入らない（単一 batch）', async () => {
    const res = await post('/internal/masters', {
      clubs: [club],
      clubSourceIds: [{ ...sourceId, sourceId: '999', clubId: '999' }],
    });
    expect(res.status).toBe(500);
    const n = await env.DB.prepare('SELECT COUNT(*) AS n FROM clubs').first<{ n: number }>();
    expect(n?.n).toBe(0);
  });

  it('知らないパスは 404', async () => {
    const res = await post('/internal/nope', {});
    expect(res.status).toBe(404);
  });
});

describe('認証の適用範囲', () => {
  it('finalize は INGEST_TOKEN では通らない（トークンを用途で分離する）', async () => {
    // freeze は破壊的操作のため FINALIZE_TOKEN を使う（詳細設計 3.4）。
    // `/internal/*` に一括適用していると、ここが誤って通ってしまう。
    const res = await post('/internal/finalize', {}, { token: TOKEN });
    expect(res.status).toBe(401);
  });

  it('masters は FINALIZE_TOKEN では通らない', async () => {
    const res = await post(
      '/internal/masters',
      { clubs: [club] },
      { token: 'finalize-token', envOverride: { FINALIZE_TOKEN: 'finalize-token' } },
    );
    expect(res.status).toBe(401);
  });
});

describe('SQL の組み立て（CLAUDE.md 絶対ルール4）', () => {
  it('値は必ず bind される。SQL に見える文字列も literal として保存される', async () => {
    // 文字列連結で SQL を組んでいれば、ここでテーブルが消えるか構文エラーになる
    const nasty = "'); DROP TABLE clubs; --";
    const res = await post('/internal/masters', {
      clubs: [{ id: 'inj-1', slug: 'inj-1', name: nasty }],
    });
    expect(res.status).toBe(200);
    const row = await env.DB.prepare('SELECT name FROM clubs WHERE id = ?')
      .bind('inj-1').first<{ name: string }>();
    expect(row?.name).toBe(nasty);
    // テーブルが残っていること
    const n = await env.DB.prepare('SELECT COUNT(*) AS n FROM clubs').first<{ n: number }>();
    expect(n?.n).toBe(1);
  });
});
