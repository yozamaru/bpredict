/**
 * 応答のキー構造が `contracts/public-shapes.json` と一致すること（詳細設計 3.7）。
 *
 * **静的JSON と公開APIで形を1つに保つための関門である。** 書き出す側は Python、
 * API は TypeScript で同じコードを共有できないため、キー構造だけを契約ファイルに
 * 固定し、両側のテストがそれを読む。**片方だけを直すと必ず落ちる。**
 *
 * 契約は `batch/tests/test_static_json.py` も読む。形を変える判断はありうるが、
 * その変更は必ず両側に届く。
 */
import { env } from 'cloudflare:test';
import { beforeAll, beforeEach, describe, expect, it } from 'vitest';

// **`?raw` で読む。** JSON インポートのために tsconfig を緩めない
// （`vite/client` の型は tests/env.d.ts が既に参照している）。
import contractRaw from '../../contracts/public-shapes.json?raw';
import { applyMigrations, get, resetAll, seedGame, type Seed } from './helpers';

beforeAll(async () => { await applyMigrations(); });
beforeEach(async () => { await resetAll(); });

const CONTRACT = JSON.parse(contractRaw) as Record<string, { paths: string[] }>;

/**
 * キーのパスを集める。オブジェクトは `.`、配列は `[]` を挟む（詳細設計 3.7）。
 * **値は見ない** — 値の範囲は各側のテストが個別に見る。
 */
function keyPaths(value: unknown, prefix = ''): Set<string> {
  if (Array.isArray(value)) {
    if (value.length === 0) return new Set([`${prefix}[]`]);
    const paths = new Set<string>();
    for (const child of value) for (const p of keyPaths(child, `${prefix}[]`)) paths.add(p);
    return paths;
  }
  if (value !== null && typeof value === 'object') {
    const paths = new Set<string>();
    for (const [key, child] of Object.entries(value)) {
      for (const p of keyPaths(child, prefix ? `${prefix}.${key}` : key)) paths.add(p);
    }
    return paths;
  }
  return new Set([prefix]);
}

function sorted(paths: Set<string>): string[] {
  return [...paths].sort();
}

function contract(shape: string): string[] {
  const entry = CONTRACT[shape];
  expect(entry, `契約に ${shape} がない`).toBeDefined();
  return [...entry!.paths].sort();
}

/** 契約との完全一致は「すべてが埋まった標本」で見る（詳細設計 3.7）。 */
async function seedFullyPopulated(): Promise<Seed> {
  const s = await seedGame({ tipoffAt: '2026-09-26T10:05:00Z', status: 'FINISHED' });
  const predictionId = `pred-${s.gameId}`;
  await env.DB.batch([
    env.DB.prepare(
      `INSERT INTO club_seasons (club_id,season_id,name,short_name,league)
       VALUES (?,?,?,?,'PREMIER'),(?,?,?,?,'PREMIER')`,
    ).bind(s.homeId, s.seasonId, '架空ホームクラブ', '架空H',
           s.awayId, s.seasonId, '架空アウェイクラブ', '架空A'),
    env.DB.prepare('INSERT INTO venues (id,name) VALUES (?,?)')
      .bind(`v-${s.gameId}`, '架空アリーナ'),
    env.DB.prepare(
      `UPDATE games SET home_score = 88, away_score = 81,
         venue_id = ?, venue_name_at_game = ? WHERE id = ?`,
    ).bind(`v-${s.gameId}`, '架空アリーナ（当時）', s.gameId),
    env.DB.prepare(
      `INSERT INTO player_seasons (player_id,season_id,club_id,number,position,roster_type)
       VALUES (?,?,?,'0','PG','JP')`,
    ).bind(s.playerId, s.seasonId, s.homeId),
  ]);
  await env.DB.prepare(
    `INSERT INTO predictions
       (id,game_id,season_id,model_version,revision,run_id,predicted_at,as_of,data_as_of,
        home_win_prob,pred_margin,pred_total,pred_home_score,pred_away_score,
        is_provisional,is_final,is_active,feature_snapshot)
     VALUES (?,?,?,?,1,'run-1','2026-09-26T00:00:00Z','2026-09-26T10:05:00Z',
             '2026-09-25T12:00:00Z',0.68,6,162,84,78,0,1,0,'{}')`,
  ).bind(predictionId, s.gameId, s.seasonId, s.modelVersion).run();
  await env.DB.batch([
    env.DB.prepare(
      `INSERT INTO prediction_reasons
         (prediction_id,rank,group_key,label_ja,value_text,favors,contribution,base_value)
       VALUES (?,1,'TEAM_STRENGTH','チーム力の差','＋82ポイント','HOME',0.42,0.1),
              (?,2,'SCHEDULE','アウェイの休養','中0日','HOME',0.11,0.1)`,
    ).bind(predictionId, predictionId),
    env.DB.prepare(
      `INSERT INTO player_predictions
         (id,prediction_id,game_id,player_id,club_id,model_version,revision,predicted_at,
          avail_prob,pred_minutes,pred_fg2a,pred_fg3a,pred_fta,
          pred_fg2_pct,pred_fg3_pct,pred_ft_pct,
          pred_oreb,pred_dreb,pred_ast,pred_tov,pred_stl,pred_blk,pred_pf,pred_fd,
          err_minutes,err_pts,err_reb,err_ast,is_provisional,is_final,is_active)
       VALUES (?,?,?,?,?,?,1,'2026-09-26T00:00:00Z',
               0.95,31.2,7.8,5.3,3.9,0.526,0.434,0.846,
               0.6,2.5,6.1,2.2,1.1,0.3,2.4,3.1,
               5.8,4.8,2.1,1.5,0,1,0)`,
    ).bind(`pp-${s.gameId}`, predictionId, s.gameId, s.playerId, s.homeId, s.modelVersion),
    env.DB.prepare(
      `INSERT INTO prediction_results
         (prediction_id,game_id,season_id,model_version,home_win_prob,prob_bucket,outcome,
          predicted_home_win,actual_home_win,is_correct,brier,score_mae,was_provisional)
       VALUES (?,?,?,?,0.68,6,'WIN',1,1,1,0.1024,3,0)`,
    ).bind(predictionId, s.gameId, s.seasonId, s.modelVersion),
    env.DB.prepare(
      `INSERT INTO accuracy_summary (scope,scope_key,model_version,n,accuracy,brier,actual_rate)
       VALUES ('OVERALL','all','',312,0.682,0.204,NULL),
              ('MODEL',?,?,312,0.682,0.204,NULL),
              ('BUCKET','60-70%','',42,0.690,0.204,0.690)`,
    ).bind(s.modelVersion, s.modelVersion),
  ]);
  return s;
}

describe('公開APIの応答は契約ファイルのキー構造に一致する', () => {
  it('GET /games/:gameId（= games/<id>.json）', async () => {
    const s = await seedFullyPopulated();
    const res = await get(`/games/${s.gameId}`, { token: null });
    expect(res.status).toBe(200);
    expect(sorted(keyPaths(await res.json()))).toEqual(contract('gameDetail'));
  });

  it('GET /games?date=（= today.json / schedule/<date>.json）', async () => {
    const s = await seedFullyPopulated();
    // 日付はシーズンの範囲内。**範囲外は D1 に触れる前に 404**（詳細設計 3.2）
    const res = await get(`/games?date=${'2026-09-22'}`, { token: null });
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(sorted(keyPaths(body))).toEqual(contract('gamesByDate'));
    expect(s.gameId).toBeTruthy();
  });
});

describe('契約ファイル自身の検査', () => {
  it('両側が読む形（shape ごとに paths を持つ）である', () => {
    for (const shape of ['gamesByDate', 'gameDetail', 'meta']) {
      expect(Array.isArray(CONTRACT[shape]?.paths), `${shape}.paths`).toBe(true);
      expect(CONTRACT[shape]!.paths.length).toBeGreaterThan(0);
    }
  });

  it('パスに重複がない', () => {
    for (const [shape, entry] of Object.entries(CONTRACT)) {
      if (!entry?.paths) continue;
      expect(new Set(entry.paths).size, shape).toBe(entry.paths.length);
    }
  });
});
