import { applyD1Migrations, createExecutionContext, env } from 'cloudflare:test';

import worker from '../src/index';

/** テストで使う Bearer。実トークンは Workers Secret から読む（ここでは注入する）。 */
export const TOKEN = 'test-ingest-token';
export const FINALIZE_TOKEN = 'test-finalize-token';

export function withToken(extra: Record<string, unknown> = {}) {
  return { ...env, INGEST_TOKEN: TOKEN, FINALIZE_TOKEN, ...extra };
}

export async function post(
  path: string,
  body: unknown,
  init: { token?: string | null; envOverride?: Record<string, unknown> } = {},
) {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  const token = init.token === undefined ? TOKEN : init.token;
  if (token !== null) headers.Authorization = `Bearer ${token}`;
  const req = new Request(`https://example.invalid${BASE}${path}`, {
    method: 'POST',
    headers,
    body: typeof body === 'string' ? body : JSON.stringify(body),
  });
  const e = init.envOverride ? { ...withToken(), ...init.envOverride } : withToken();
  return worker.fetch(req, e, createExecutionContext());
}

/**
 * `db/migrations/*.sql` をそのまま適用する。
 *
 * **テストのスキーマを二重管理しない**（CLAUDE.md）。DDL は
 * `vitest.config.ts` が `readD1Migrations('../db/migrations')` で読み、
 * `TEST_MIGRATIONS` として注入する。
 */
export async function applyMigrations() {
  await applyD1Migrations(env.DB, env.TEST_MIGRATIONS);
}

/** GET 用。内部エンドポイントはすべて Bearer 必須。 */
/** ベースパス。実装とテストで二重に書かない（詳細設計 3.1） */
export const BASE = '/api/v1';

export async function get(path: string, init: { token?: string | null } = {}) {
  const headers: Record<string, string> = {};
  const token = init.token === undefined ? TOKEN : init.token;
  if (token !== null) headers.Authorization = `Bearer ${token}`;
  const req = new Request(`https://example.invalid${BASE}${path}`, { method: 'GET', headers });
  return worker.fetch(req, withToken(), createExecutionContext());
}

/**
 * 消せるものだけ消す。
 *
 * **凍結済み（`is_final = 1`）の予測は DELETE できない。** トリガが拒否するのが
 * 設計どおりの挙動であり（詳細設計 1.8）、テストの後片付けのために曲げない。
 * 代わりに各テストが一意なIDを使い、残骸と干渉しないようにする。
 */
export async function resetAll() {
  const live = '(SELECT id FROM predictions WHERE is_final = 0)';
  await env.DB.batch([
    ...['prediction_results', 'prediction_model_bundle', 'prediction_reasons',
        'prediction_team_targets'].map((tbl) =>
      env.DB.prepare(`DELETE FROM ${tbl} WHERE prediction_id IN ${live}`)),
    env.DB.prepare('DELETE FROM player_predictions WHERE is_final = 0'),
    env.DB.prepare('DELETE FROM predictions WHERE is_final = 0'),
    env.DB.prepare('DELETE FROM accuracy_summary'),
    env.DB.prepare('DELETE FROM ingestion_logs'),
    env.DB.prepare('DELETE FROM game_entries'),
    env.DB.prepare('DELETE FROM player_game_stats'),
    env.DB.prepare('DELETE FROM team_game_stats'),
    env.DB.prepare('DELETE FROM team_ratings'),
    env.DB.prepare('DELETE FROM team_games'),
    env.DB.prepare('DELETE FROM games WHERE id NOT IN (SELECT game_id FROM predictions)'),
    env.DB.prepare(
      'DELETE FROM model_versions WHERE version NOT IN (SELECT model_version FROM predictions)'),
    env.DB.prepare('DELETE FROM player_seasons'),
    env.DB.prepare(
      'DELETE FROM players WHERE id NOT IN (SELECT player_id FROM player_predictions)'),
    env.DB.prepare('DELETE FROM club_seasons'),
    env.DB.prepare('DELETE FROM club_source_ids'),
    env.DB.prepare(`DELETE FROM clubs WHERE id NOT IN (
        SELECT home_club_id FROM games UNION SELECT away_club_id FROM games
        UNION SELECT club_id FROM player_predictions)`),
    env.DB.prepare('DELETE FROM venue_source_keys'),
    env.DB.prepare('DELETE FROM venue_revisions'),
    env.DB.prepare('DELETE FROM venues'),
    env.DB.prepare('DELETE FROM seasons WHERE id NOT IN (SELECT season_id FROM games)'),
  ]);
}

/**
 * 予測を入れるのに必要な最小の下地（season / club / player / game / model_version）。
 *
 * **呼び出しごとに一意なIDを使う。** 凍結済みの予測は消せないため、テスト間で
 * IDを使い回すと残骸と衝突する。
 *
 * `predictions.model_version` は `model_versions(version)` を参照する。
 * モデル行がないと FK 違反で 500 になる。
 */
let seq = 0;

export type Seed = {
  gameId: string; seasonId: string; homeId: string; awayId: string;
  playerId: string; modelVersion: string;
};

export async function seedGame(opts: { tipoffAt: string; status?: string }): Promise<Seed> {
  const n = ++seq;
  const s: Seed = {
    gameId: `g-${n}`, seasonId: `s-${n}`, homeId: `h-${n}`, awayId: `a-${n}`,
    playerId: `p-${n}`, modelVersion: `winner-v${n}.0.0`,
  };
  await env.DB.batch([
    env.DB
      .prepare('INSERT INTO seasons (id,label,league,start_date,end_date) VALUES (?,?,?,?,?)')
      .bind(s.seasonId, '2026-27', 'PREMIER', '2026-09-22', '2027-05-02'),
    env.DB.prepare('INSERT INTO clubs (id,slug,name) VALUES (?,?,?),(?,?,?)')
      .bind(s.homeId, `home-${n}`, '架空ホーム', s.awayId, `away-${n}`, '架空アウェイ'),
    env.DB.prepare('INSERT INTO players (id,name) VALUES (?,?)').bind(s.playerId, '架空 選手'),
    env.DB
      .prepare(
        `INSERT INTO model_versions
           (version,model_type,algo,trained_at,train_rows,train_range,eval_window,params,feature_list)
         VALUES (?,'WINNER','lightgbm','2026-09-01T00:00:00Z',6120,
                 '2016-17..2025-26','2024-25..2025-26','{}','["elo_diff"]')`,
      )
      .bind(s.modelVersion),
    env.DB
      .prepare(
        `INSERT INTO games (id,season_id,league,competition,game_date,tipoff_at,
           home_club_id,away_club_id,status) VALUES (?,?,?,?,?,?,?,?,?)`,
      )
      .bind(s.gameId, s.seasonId, 'PREMIER', 'REGULAR', '2026-09-22', opts.tipoffAt,
            s.homeId, s.awayId, opts.status ?? 'SCHEDULED'),
  ]);
  return s;
}

/** 予測1本分の最小ペイロード。 */
export function predictionPayload(s: Seed, over: Record<string, unknown> = {}) {
  return {
    gameId: s.gameId,
    seasonId: s.seasonId,
    modelVersion: s.modelVersion,
    runId: 'run-1',
    predictedAt: '2026-09-21T21:00:00Z',
    asOf: '2026-09-22T10:05:00Z',
    dataAsOf: '2026-09-21T12:00:00Z',
    homeWinProb: 0.68,
    predMargin: 6, predTotal: 162, predHomeScore: 84, predAwayScore: 78,
    isProvisional: 1,
    featureSnapshot: '{"elo_diff":82}',
    ...over,
  };
}
