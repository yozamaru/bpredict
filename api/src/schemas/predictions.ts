/** 予測の追記の入力スキーマ。1リクエスト1試合（`post_prediction(g, ...)` に対応）。 */
import { z } from 'zod';

const ID = z.string().min(1).max(64);
const TS = z.string().min(1).max(64);
const prob = z.number().min(0).max(1);
const nonNeg = z.number().min(0);
const zeroOne = z.union([z.literal(0), z.literal(1)]);

/**
 * 整合化の目標値（`prediction_team_targets`。要件 6.8.5）。
 *
 * **「成功数」ではなく「試投数 + 成功率」で持つ。** 成功数を直接持つと約2%の確率で
 * `成功数 > 試投数` という**実行不能な目標**が生まれ、どんなアルゴリズムでも到達
 * できない。率（0〜1）と試投数（非負）の積なら `成功数 <= 試投数` が構造的に成り立つ。
 *
 * **ここに入るのは `reconcile_team_targets()` を通した後の値である** — TeamRates の
 * 生出力では導出得点が予想スコアと一致しない（詳細設計 2.4 の前段）。
 */
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

/**
 * 選手1人の予測（整合化**後**の値）。
 *
 * **成功数・得点・リバウンド合計の列を持たない。** すべて率と試投数から導出できる
 * （詳細設計 1.5）。列として持つと、整合化の後に恒等式が崩れても検出できない。
 *
 * **`plusMinus` を予測しない**（要件 6.8.3）。1試合の個人プラスマイナスは
 * MAE 9〜12点規模で、「チーム得点差 × 出場時間シェア」以上の情報を持たない。
 */
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

/**
 * なぜこの予測になったか（`prediction_reasons`。要件 6.9）。
 *
 * **要因グループ単位に集約した寄与**であり、個別の列は出さない。`contribution` は
 * **ログオッズ空間の値**で、`%` を付けて画面に出さない（`%` は勝率専用）。
 *
 * `factorSchema`（この予測に使った項目）とは別物である — あちらは「何を見たか」で、
 * 有利不利を主張しない（詳細設計 2.7.2）。
 */
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

/**
 * この予測に使った項目（詳細設計 2.7.2 の `prediction_factors`）。
 *
 * **`reasonSchema` と別に持つ。** あちらは寄与の主張で `favors` が必須だが、
 * こちらは「値が大きい側」であり**向きを持たない列がある**（`series_game_no` /
 * `entry_is_official` / 差が 0）。
 *
 * `rank` の上限は特徴量の列数で決まる。現在21列で、**上限を40にしてある** —
 * 列を足したときに Zod で落ちないようにするためで、`reasonSchema` の20
 * （要因グループは4つしかない）とは別の数である。
 */
export const factorSchema = z
  .object({
    rank: z.number().int().min(1).max(40),
    groupKey: z.enum(['TEAM_STRENGTH', 'SCHEDULE', 'PLAYER', 'VENUE']),
    labelJa: z.string().min(1).max(128),
    valueText: z.string().min(1).max(128),
    // **「有利な側」ではない。** 向きを持たない列は null
    larger: z.enum(['HOME', 'AWAY']).nullable(),
  })
  .strict();

/**
 * 推論に使ったモデル一式（`prediction_model_bundle`。詳細設計 1.6）。
 *
 * **`predictions.model_version` だけでは出自を復元できない。** 1本の予測は
 * Winner / Margin / Total / TeamRate×14 / PlayerAvail / PlayerMin / PlayerRate×14 の
 * 合成で、単一の列にはチームモデルの版しか書けない。**最大33行**になる。
 */
export const bundleSchema = z
  .object({
    modelType: z.enum([
      'WINNER', 'MARGIN', 'TOTAL', 'TEAM_RATE', 'PLAYER_AVAIL', 'PLAYER_MIN', 'PLAYER_RATE',
    ]),
    target: z.string().max(32).optional(),
    modelVersion: ID,
  })
  .strict();

/**
 * `POST /internal/predictions` の本文。**1リクエスト1試合。**
 *
 * この口が守る関門（詳細設計 3.4）— Bearer の定数時間比較 / Zod の型と値域 /
 * **`tipoff_at <= now` の試合を 409 で拒否** / `is_final = 1` を UPDATE しない /
 * 旧行の非活性化と新行の挿入を**単一 `batch()`** に入れる。
 *
 * **`teamTargets` は2件か0件である**（`refine` が要求する）。1件は片側だけ
 * 整合化した状態であり、原理的に誤りである。
 */
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
    factors: z.array(factorSchema).max(40).optional(),
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
