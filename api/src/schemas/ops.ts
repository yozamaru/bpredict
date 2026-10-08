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
    /**
     * 予想スコアの誤差（**1チームあたり**の平均絶対誤差。詳細設計 1.6）。
     *
     * **上限を 1 にしない** — 的中率や Brier と違い点数であり、値域は得点の
     * 大きさで決まる。負にはならない。母数が `n` と食い違う場合は集計側が
     * null にする（4.12。母数の列を2つ持たない）。
     */
    scoreMae: z.number().min(0).nullable().optional(),
    /**
     * **その帯の的中率。`BUCKET` 行だけが持つ**（詳細設計 1.6 / 4.12）。
     *
     * 他のスコープでは `accuracy` がそのまま的中率であり、**同じ値を2列に
     * 持たない**。したがってここは null で届く。
     *
     * **`actualRate` で代用できない** — あれはホームが勝った割合で、50%未満の
     * 帯では的中率と符号が逆になる（本番で6試合中3試合が逆に出た。1.6）。
     */
    hitRate: z.number().min(0).max(1).nullable().optional(),
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

/**
 * 実績の集計（詳細設計 1.9 / 3.4）。
 *
 * **洗い替えない。upsert だけである** — 約4,650行は1リクエスト（160行）に
 * 収まらず、分割すると後の DELETE が前の INSERT を消す（基本設計 3.2）。
 */
const counts = {
  fg2m: z.number().int().min(0),
  fg2a: z.number().int().min(0),
  fg3m: z.number().int().min(0),
  fg3a: z.number().int().min(0),
  ftm: z.number().int().min(0),
  fta: z.number().int().min(0),
  oreb: z.number().int().min(0),
  dreb: z.number().int().min(0),
  ast: z.number().int().min(0),
  tov: z.number().int().min(0),
  stl: z.number().int().min(0),
  blk: z.number().int().min(0),
  pf: z.number().int().min(0),
  fd: z.number().int().min(0),
};

export const playerStatSchema = z
  .object({
    playerId: ID,
    scope: z.enum(['SEASON', 'CAREER']),
    scopeKey: z.string().max(64),
    /** 季の行はクラブ別。**CAREER では空文字**（NULL にしない。詳細設計 1.9）。 */
    clubId: z.string().max(128),
    games: z.number().int().min(0),
    gamesStarted: z.number().int().min(0),
    minutes: z.number().min(0),
    ...counts,
    pts: z.number().int().min(0),
  })
  .strict()
  .refine(
    (r) =>
      r.scope === 'CAREER'
        ? r.scopeKey === '' && r.clubId === ''
        : r.scopeKey !== '' && r.clubId !== '',
    { message: 'CAREER は scopeKey と clubId が空、SEASON はどちらも空でないこと' },
  )
  .refine((r) => r.gamesStarted <= r.games, { message: 'gamesStarted が games を超えている' })
  .refine((r) => r.fg2m <= r.fg2a && r.fg3m <= r.fg3a && r.ftm <= r.fta, {
    message: '成功数が試投数を超えている',
  });

export const teamStatSchema = z
  .object({
    clubId: ID,
    scope: z.enum(['SEASON', 'CAREER']),
    scopeKey: z.string().max(64),
    games: z.number().int().min(0),
    wins: z.number().int().min(0),
    pointsFor: z.number().int().min(0),
    pointsAgainst: z.number().int().min(0),
    /** ボックススコアの母数。**`games` と別に持つ**（詳細設計 1.9）。 */
    statGames: z.number().int().min(0),
    ...counts,
  })
  .strict()
  .refine((r) => (r.scope === 'CAREER' ? r.scopeKey === '' : r.scopeKey !== ''), {
    message: 'CAREER は scopeKey が空、SEASON は空でないこと',
  })
  .refine((r) => r.wins <= r.games, { message: 'wins が games を超えている' })
  .refine((r) => r.fg2m <= r.fg2a && r.fg3m <= r.fg3a && r.ftm <= r.fta, {
    message: '成功数が試投数を超えている',
  });

/** どちらも省略可。**両方空のリクエストは拒否する**（詳細設計 3.4）。 */
export const statSummaryBody = z
  .object({
    playerStats: z.array(playerStatSchema).optional(),
    teamStats: z.array(teamStatSchema).optional(),
  })
  .strict()
  .refine((b) => (b.playerStats?.length ?? 0) + (b.teamStats?.length ?? 0) > 0, {
    message: '1行も書かないリクエストは受け付けない',
  });
