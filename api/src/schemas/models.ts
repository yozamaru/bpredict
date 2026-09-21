/** モデル登録の入力スキーマ。 */
import { z } from 'zod';

const ID = z.string().min(1).max(128);

export const MODEL_TYPES = [
  'WINNER', 'MARGIN', 'TOTAL', 'TEAM_RATE', 'PLAYER_AVAIL', 'PLAYER_MIN', 'PLAYER_RATE',
] as const;

/**
 * `artifact_text` の上限 1.5 MiB。
 *
 * D1 の1行上限は 2,000,000 バイト。**アプリ層でも同じ判定を行い、DBトリガと
 * 二重化する**（詳細設計 1.6 / 受け入れ基準 A-18）。
 */
export const ARTIFACT_MAX_BYTES = 1_572_864;

export const modelBody = z
  .object({
    version: ID,
    modelType: z.enum(MODEL_TYPES),
    target: z.string().max(32).optional(),
    league: z.string().max(16).optional(),
    winProbSource: z.enum(['WINNER', 'MARGIN']).nullable().optional(),
    marginSigma: z.number().min(0).nullable().optional(),
    algo: z.string().min(1).max(64),
    trainedAt: z.string().min(1).max(64),
    trainRows: z.number().int().min(0),
    trainRange: z.string().min(1).max(64),
    evalWindow: z.string().min(1).max(64),
    params: z.string().min(2),                 // JSON。seed 系を必ず含める
    featureList: z.string().min(2),            // JSON 配列
    featureNullRates: z.string().nullable().optional(),
    cvAccuracy: z.number().nullable().optional(),
    cvBrier: z.number().nullable().optional(),
    cvLogloss: z.number().nullable().optional(),
    cvEce: z.number().nullable().optional(),
    baselineHomeAccuracy: z.number().nullable().optional(),
    baselineEloBrier: z.number().nullable().optional(),
    artifactText: z.string().nullable().optional(),
    artifactSha256: z.string().max(64).nullable().optional(),
    calibrator: z.string().nullable().optional(),
    /** 登録と同時に有効化するか。`uq_model_active` が整合を保証する。 */
    activate: z.boolean().optional(),
    notes: z.string().max(1024).nullable().optional(),
  })
  .strict();
