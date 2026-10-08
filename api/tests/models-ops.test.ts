import { env } from 'cloudflare:test';
import { beforeAll, beforeEach, describe, expect, it } from 'vitest';

import { applyMigrations, get, post, resetAll, seedGame } from './helpers';
import { ARTIFACT_MAX_BYTES } from '../src/schemas/models';

beforeAll(async () => { await applyMigrations(); });
beforeEach(async () => { await resetAll(); });

const model = (over: Record<string, unknown> = {}) => ({
  version: 'winner-t1', modelType: 'WINNER', algo: 'lightgbm',
  trainedAt: '2026-09-01T00:00:00Z', trainRows: 6120,
  trainRange: '2016-17..2025-26', evalWindow: '2024-25..2025-26',
  params: '{"seed":42}', featureList: '["elo_diff"]',
  ...over,
});

describe('モデルの登録（受け入れ基準 A-18）', () => {
  it('1.5MB を超える artifact_text は 400（アプリ層とトリガで二重化）', async () => {
    const res = await post('/internal/models', model({
      artifactText: 'a'.repeat(ARTIFACT_MAX_BYTES + 1),
    }));
    expect(res.status).toBe(400);
    const n = await env.DB.prepare('SELECT COUNT(*) AS n FROM model_versions WHERE version = ?')
      .bind('winner-t1').first<{ n: number }>();
    expect(n?.n).toBe(0);
  });

  it('ちょうど上限なら通る', async () => {
    const res = await post('/internal/models', model({
      artifactText: 'a'.repeat(ARTIFACT_MAX_BYTES),
    }));
    expect(res.status).toBe(200);
    expect((await res.json<{ data: { artifactBytes: number } }>()).data.artifactBytes)
      .toBe(ARTIFACT_MAX_BYTES);
  });

  it('マルチバイトでもバイト数で測る（文字数ではない）', async () => {
    // 「あ」は UTF-8 で3バイト。文字数で測っていると通ってしまう
    const res = await post('/internal/models', model({
      artifactText: 'あ'.repeat(Math.floor(ARTIFACT_MAX_BYTES / 3) + 1),
    }));
    expect(res.status).toBe(400);
  });

  it('activate で有効モデルが1本に保たれる（uq_model_active）', async () => {
    await post('/internal/models', model({ version: 'winner-t1', activate: true }));
    await post('/internal/models', model({ version: 'winner-t2', activate: true }));
    const rows = await env.DB
      .prepare("SELECT version FROM model_versions WHERE is_active = 1 AND model_type = 'WINNER'")
      .all<{ version: string }>();
    expect(rows.results.map((r) => r.version)).toEqual(['winner-t2']);
  });

  it('未知の model_type は 400', async () => {
    expect((await post('/internal/models', model({ modelType: 'UNKNOWN' }))).status).toBe(400);
  });
});

describe('モデルの読み出し', () => {
  it('一覧は artifact_text を含めず、バイト数だけ返す', async () => {
    await post('/internal/models', model({ artifactText: 'tree\nversion=v4\n', activate: true }));
    const res = await get('/internal/models/active');
    expect(res.status).toBe(200);
    const body = await res.json<{ data: { models: Record<string, unknown>[] } }>();
    expect(body.data.models).toHaveLength(1);
    const m = body.data.models[0]!;
    expect(m).not.toHaveProperty('artifactText');
    expect(m.artifactBytes).toBe(16);   // 'tree\nversion=v4\n'
  });

  it('本体は1本ずつ取れる', async () => {
    await post('/internal/models', model({ artifactText: 'tree\nversion=v4\n' }));
    const res = await get('/internal/models/winner-t1/artifact');
    expect(res.status).toBe(200);
    const body = await res.json<{ data: { artifactText: string } }>();
    expect(body.data.artifactText).toBe('tree\nversion=v4\n');
  });

  it('無いモデルは 404', async () => {
    expect((await get('/internal/models/missing/artifact')).status).toBe(404);
  });

  it('現行モデルの評価値が取れる', async () => {
    await post('/internal/models', model({ cvBrier: 0.2041, cvEce: 0.031, activate: true }));
    const res = await get('/internal/metrics/active?modelType=WINNER&target=&league=PREMIER');
    expect(res.status).toBe(200);
    const body = await res.json<{ data: { version: string; cvBrier: number } }>();
    expect(body.data).toMatchObject({ version: 'winner-t1', cvBrier: 0.2041 });
  });

  it('有効モデルが無ければ 404', async () => {
    expect((await get('/internal/metrics/active')).status).toBe(404);
  });
});

describe('照合の登録（受け入れ基準 A-04）', () => {
  async function frozenPrediction() {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    await post('/internal/predictions', {
      gameId: s.gameId, seasonId: s.seasonId, modelVersion: s.modelVersion, runId: 'r',
      predictedAt: '2026-09-21T21:00:00Z', asOf: '2026-09-22T10:05:00Z',
      dataAsOf: '2026-09-21T12:00:00Z', homeWinProb: 0.68, featureSnapshot: '{}',
    });
    const id = (await env.DB.prepare('SELECT id FROM predictions WHERE game_id = ?')
      .bind(s.gameId).first<{ id: string }>())!.id;
    return { s, id };
  }

  it('中止・延期は VOID として入り、actual_home_win が NULL でも通る', async () => {
    const { s, id } = await frozenPrediction();
    const res = await post('/internal/evaluate', {
      results: [{
        predictionId: id, gameId: s.gameId, seasonId: s.seasonId, modelVersion: s.modelVersion,
        homeWinProb: 0.68, probBucket: 6, outcome: 'VOID', wasProvisional: 1,
      }],
    });
    expect(res.status).toBe(200);
    const row = await env.DB.prepare('SELECT outcome, actual_home_win AS a FROM prediction_results')
      .first<{ outcome: string; a: number | null }>();
    expect(row).toEqual({ outcome: 'VOID', a: null });
  });

  it('VOID に actualHomeWin を入れたら 400', async () => {
    const { s, id } = await frozenPrediction();
    const res = await post('/internal/evaluate', {
      results: [{
        predictionId: id, gameId: s.gameId, seasonId: s.seasonId, modelVersion: s.modelVersion,
        homeWinProb: 0.68, probBucket: 6, outcome: 'VOID', actualHomeWin: 1, wasProvisional: 1,
      }],
    });
    expect(res.status).toBe(400);
  });

  it('冪等（3回流しても1行のまま）', async () => {
    const { s, id } = await frozenPrediction();
    const body = {
      results: [{
        predictionId: id, gameId: s.gameId, seasonId: s.seasonId, modelVersion: s.modelVersion,
        homeWinProb: 0.68, probBucket: 6, outcome: 'WIN', actualHomeWin: 1, isCorrect: 1,
        brier: 0.1024, wasProvisional: 0,
      }],
    };
    for (let i = 0; i < 3; i += 1) expect((await post('/internal/evaluate', body)).status).toBe(200);
    const n = await env.DB.prepare('SELECT COUNT(*) AS n FROM prediction_results').first<{ n: number }>();
    expect(n?.n).toBe(1);
  });
});

describe('照合済みの結果の読み出し（詳細設計 3.4 / 4.12）', () => {
  async function evaluated(n: number) {
    const s = await seedGame({ tipoffAt: '2099-01-01T10:05:00Z' });
    for (let i = 0; i < n; i += 1) {
      const id = `p${String(i).padStart(3, '0')}`;
      // **FK を満たすため親も入れる。** `is_active = 0` にするのは、照合済みの
      // 予測は再推論で非活性化されているのが普通であり、部分ユニーク索引
      // （`uq_pred_active`）に当たらないためでもある
      await env.DB.prepare(
        `INSERT INTO predictions
           (id, game_id, season_id, model_version, revision, run_id, predicted_at,
            as_of, data_as_of, home_win_prob, feature_snapshot, is_active)
         VALUES (?, ?, ?, ?, ?, 'r', '2026-09-21T21:00:00Z', '2026-09-22T10:05:00Z',
                 '2026-09-21T12:00:00Z', 0.68, '{}', 0)`,
      ).bind(id, s.gameId, s.seasonId, s.modelVersion, i + 1).run();
      await env.DB.prepare(
        `INSERT INTO prediction_results
           (prediction_id, game_id, season_id, model_version, home_win_prob,
            prob_bucket, outcome, predicted_home_win, actual_home_win,
            is_correct, brier, score_mae, was_provisional)
         VALUES (?, ?, ?, ?, 0.68, 6, 'WIN', 1, 1, 1, 0.1024, 3.5, 0)`,
      ).bind(id, s.gameId, s.seasonId, s.modelVersion).run();
    }
    return s;
  }

  it('全件を camelCase で返す', async () => {
    await evaluated(1);
    const res = await get('/internal/results');
    expect(res.status).toBe(200);
    const body = await res.json<{ data: { count: number; next: string | null; results: Record<string, unknown>[] } }>();
    expect(body.data.count).toBe(1);
    expect(body.data.next).toBeNull();
    // **バッチが `Result` に戻すためのキーがすべてある**（`_result_of` と対になる）
    expect(Object.keys(body.data.results[0]!).sort()).toEqual([
      'actualHomeWin', 'brier', 'gameId', 'homeWinProb', 'isCorrect', 'modelVersion',
      'outcome', 'predictedHomeWin', 'predictionId', 'probBucket', 'scoreMae',
      'seasonId', 'wasProvisional',
    ]);
  });

  it('カーソルで続きが取れる。**`next` は続きがあるときだけ入る**', async () => {
    await evaluated(5);
    const first = await (await get(`/internal/results?limit=2`))
      .json<{ data: { next: string | null; results: { predictionId: string }[] } }>();
    expect(first.data.results.map((r) => r.predictionId)).toEqual(['p000', 'p001']);
    expect(first.data.next).toBe('p001');

    const second = await (await get(`/internal/results?limit=2&after=p001`))
      .json<{ data: { next: string | null; results: { predictionId: string }[] } }>();
    expect(second.data.results.map((r) => r.predictionId)).toEqual(['p002', 'p003']);

    const last = await (await get(`/internal/results?limit=2&after=p003`))
      .json<{ data: { next: string | null; results: { predictionId: string }[] } }>();
    expect(last.data.results.map((r) => r.predictionId)).toEqual(['p004']);
    expect(last.data.next).toBeNull();
  });

  it('1件も無ければ空。**空配列を返し、404 にしない**', async () => {
    const body = await (await get('/internal/results'))
      .json<{ data: { count: number; next: string | null } }>();
    expect(body.data).toMatchObject({ count: 0, next: null });
  });

  it('Bearer なしで 401', async () => {
    expect((await get(`/internal/results`, { token: null })).status).toBe(401);
  });
});

describe('集計の洗い替え', () => {
  it('model_version を省くと空文字が入る（NULL にしない）', async () => {
    const res = await post('/internal/summary', {
      rows: [{ scope: 'OVERALL', scopeKey: 'all', n: 312, accuracy: 0.682, brier: 0.204 }],
    });
    expect(res.status).toBe(200);
    const row = await env.DB.prepare('SELECT model_version AS m FROM accuracy_summary')
      .first<{ m: string }>();
    expect(row?.m).toBe('');
  });

  it('毎回すべて消してから入れる', async () => {
    await post('/internal/summary', {
      rows: [
        { scope: 'OVERALL', scopeKey: 'all', n: 1, accuracy: 0.5, brier: 0.25 },
        { scope: 'SEASON', scopeKey: '2026-27', n: 1, accuracy: 0.5, brier: 0.25 },
      ],
    });
    await post('/internal/summary', {
      rows: [{ scope: 'OVERALL', scopeKey: 'all', n: 2, accuracy: 0.6, brier: 0.2 }],
    });
    const n = await env.DB.prepare('SELECT COUNT(*) AS n FROM accuracy_summary').first<{ n: number }>();
    expect(n?.n).toBe(1);
  });

  it('未知の scope は 400', async () => {
    const res = await post('/internal/summary', {
      rows: [{ scope: 'WEEKLY', scopeKey: 'x', n: 1, accuracy: 0.5, brier: 0.25 }],
    });
    expect(res.status).toBe(400);
  });
});

describe('実行ログ', () => {
  it('RUNNING → SUCCESS を同じ id で更新できる', async () => {
    const base = { id: 'run-1', job: 'daily_ingest', startedAt: '2026-09-21T21:00:00Z' };
    await post('/internal/log', { ...base, status: 'RUNNING' });
    await post('/internal/log', {
      ...base, status: 'SUCCESS', finishedAt: '2026-09-21T21:08:00Z', rowsAffected: 120,
    });
    const rows = await env.DB.prepare('SELECT status FROM ingestion_logs').all<{ status: string }>();
    expect(rows.results).toEqual([{ status: 'SUCCESS' }]);
  });

  it('error_message が長すぎたら 400（例外をそのまま入れさせない）', async () => {
    const res = await post('/internal/log', {
      id: 'run-2', job: 'daily_ingest', startedAt: '2026-09-21T21:00:00Z', status: 'FAILED',
      errorMessage: 'x'.repeat(257),
    });
    expect(res.status).toBe(400);
  });

  it('未知の status は 400', async () => {
    const res = await post('/internal/log', {
      id: 'run-3', job: 'x', startedAt: '2026-09-21T21:00:00Z', status: 'WEIRD',
    });
    expect(res.status).toBe(400);
  });
});
