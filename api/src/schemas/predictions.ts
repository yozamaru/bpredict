/** 予測の追記の入力スキーマ。1リクエスト1試合（`post_prediction(g, ...)` に対応）。 */
import { z } from 'zod';

const ID = z.string().min(1).max(64);
const TS = z.string().min(1).max(64);
const prob = z.number().min(0).max(1);
const nonNeg = z.number().min(0);
const zeroOne = z.union([z.literal(0), z.literal(1)]);

export const teamTargetSchema = z
  .object({
    clubId: ID,
    isHome: zeroOne,
    tgtFg2a: nonNeg, tgtFg3a: nonNeg, tgtFta: nonNeg,
    tgtFg2Pct: prob, tgtFg3Pct: prob, tgtFtPct: prob,
    tgtOreb: nonNeg, tgtDreb: nonNeg, tgtAst: nonNeg, tgtTov: nonNeg,
    tgtStl: nonNeg, tgtBlk: nonNeg, tgtPf: nonNeg, tgtFd: nonNeg,
  })
  .strict();

export const playerPredictionSchema = z
  .object({
    playerId: ID,
    clubId: ID,
    availProb: prob,
    predMinutes: nonNeg,
    predFg2a: nonNeg, predFg3a: nonNeg, predFta: nonNeg,
    predFg2Pct: prob, predFg3Pct: prob, predFtPct: prob,
    predOreb: nonNeg, predDreb: nonNeg, predAst: nonNeg, predTov: nonNeg,
    predStl: nonNeg, predBlk: nonNeg, predPf: nonNeg, predFd: nonNeg,
    // 誤差の目安は主要4項目のみ（要件 6.8.6）
    errMinutes: z.number().nullable().optional(),
    errPts: z.number().nullable().optional(),
    errReb: z.number().nullable().optional(),
    errAst: z.number().nullable().optional(),
    isProvisional: zeroOne.optional(),
  })
  .strict();

export const reasonSchema = z
  .object({
    rank: z.number().int().min(1).max(20),
    groupKey: z.enum(['TEAM_STRENGTH', 'SCHEDULE', 'PLAYER', 'VENUE']),
    labelJa: z.string().min(1).max(128),
    valueText: z.string().min(1).max(128),
    favors: z.enum(['HOME', 'AWAY']),
    // SHAP はログオッズ空間の値。画面に数値を出さない（詳細設計 2.7）
    contribution: z.number(),
    baseValue: z.number(),
  })
  .strict();

export const bundleSchema = z
  .object({
    modelType: z.enum([
      'WINNER', 'MARGIN', 'TOTAL', 'TEAM_RATE', 'PLAYER_AVAIL', 'PLAYER_MIN', 'PLAYER_RATE',
    ]),
    target: z.string().max(32).optional(),
    modelVersion: ID,
  })
  .strict();

export const predictionBody = z
  .object({
    gameId: ID,
    seasonId: ID,
    modelVersion: ID,
    runId: ID,
    predictedAt: TS,
    asOf: TS,
    dataAsOf: TS,
    homeWinProb: prob,
    predMargin: z.number().nullable().optional(),
    predTotal: z.number().nullable().optional(),
    predHomeScore: z.number().nullable().optional(),
    predAwayScore: z.number().nullable().optional(),
    isProvisional: zeroOne.optional(),
    featureSnapshot: z.string().min(2),          // JSON 文字列
    teamTargets: z.array(teamTargetSchema).max(2).optional(),
    playerPredictions: z.array(playerPredictionSchema).optional(),
    reasons: z.array(reasonSchema).optional(),
    modelBundle: z.array(bundleSchema).optional(),
  })
  .strict()
  .refine((b) => (b.teamTargets ?? []).length !== 1, {
    message: 'teamTargets はホーム・アウェイの2件か、0件（個人スタッツを破棄した試合）',
  });

/** freeze の対象指定。空なら `tipoff_at <= now` の全件を対象にする。 */
export const finalizeBody = z
  .object({ gameIds: z.array(ID).max(200).optional() })
  .strict();
