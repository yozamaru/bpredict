/** 照合・集計・ログの入力スキーマ。 */
import { z } from 'zod';

const ID = z.string().min(1).max(128);
const zeroOne = z.union([z.literal(0), z.literal(1)]);

export const resultSchema = z
  .object({
    predictionId: ID,
    gameId: ID,
    seasonId: ID,
    modelVersion: ID,
    homeWinProb: z.number().min(0).max(1),
    probBucket: z.number().int().min(0).max(9),
    /** **中止・延期は VOID とし、的中率の母数から除外する**（受け入れ基準 A-04）。 */
    outcome: z.enum(['WIN', 'LOSS', 'VOID']),
    predictedHomeWin: zeroOne.nullable().optional(),
    actualHomeWin: zeroOne.nullable().optional(),
    isCorrect: zeroOne.nullable().optional(),
    brier: z.number().nullable().optional(),
    scoreMae: z.number().nullable().optional(),
    wasProvisional: zeroOne,
  })
  .strict()
  .refine((r) => r.outcome !== 'VOID' || r.actualHomeWin === null || r.actualHomeWin === undefined, {
    message: 'VOID の行に actualHomeWin を入れない',
  });

export const evaluateBody = z
  .object({ results: z.array(resultSchema).min(1) })
  .strict();

export const summarySchema = z
  .object({
    scope: z.enum(['OVERALL', 'SEASON', 'MODEL', 'BUCKET', 'PROVISIONAL']),
    scopeKey: z.string().min(1).max(64),
    /** モデル横断の集計では空文字。**NULL にしない**（詳細設計 1.6）。 */
    modelVersion: z.string().max(128).optional(),
    n: z.number().int().min(0),
    accuracy: z.number().min(0).max(1),
    brier: z.number().min(0).max(1),
    actualRate: z.number().min(0).max(1).nullable().optional(),
    baselineAccuracy: z.number().min(0).max(1).nullable().optional(),
  })
  .strict();

/** `accuracy_summary` は日次で**洗い替える**（詳細設計 1.6）。 */
export const summaryBody = z
  .object({ rows: z.array(summarySchema) })
  .strict();

export const logBody = z
  .object({
    id: ID,
    job: z.string().min(1).max(64),
    startedAt: z.string().min(1).max(64),
    finishedAt: z.string().max(64).nullable().optional(),
    status: z.enum(['RUNNING', 'SUCCESS', 'PARTIAL', 'FAILED', 'ABORTED']),
    rowsAffected: z.number().int().nullable().optional(),
    d1RowsRead: z.number().int().nullable().optional(),
    /** **例外オブジェクトをそのまま入れない。** 型名だけ（CLAUDE.md 絶対ルール4）。 */
    errorType: z.string().max(128).nullable().optional(),
    /** 自前の短いメッセージのみ。URL やIDを含めない。 */
    errorMessage: z.string().max(256).nullable().optional(),
  })
  .strict();
