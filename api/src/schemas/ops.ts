/** 照合・集計・ログの入力スキーマ。 */
import { z } from 'zod';

const ID = z.string().min(1).max(128);
const zeroOne = z.union([z.literal(0), z.literal(1)]);

/**
 * 照合1件（`prediction_results` の1行。詳細設計 4.12）。
 *
 * **`VOID` の列は NULL にする。** `refine` が `actualHomeWin` の混入を拒否する —
 * **0 は「ホームが負けた」という意味を持ってしまう**（受け入れ基準 A-04）。
 *
 * `probBucket` は 0〜9。`p = 1.0` を10番目にしない（`min(floor(p × 10), 9)`）。
 */
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

/** `POST /internal/evaluate` の本文。`prediction_results` へ upsert するため冪等である。 */
export const evaluateBody = z
  .object({ results: z.array(resultSchema).min(1) })
  .strict();

/**
 * `accuracy_summary` の1行（詳細設計 1.6）。
 *
 * **`modelVersion` に NULL を入れない。** SQLite は主キー列の NULL 重複を許すため、
 * モデル横断の4スコープ（`OVERALL` / `SEASON` / `BUCKET` / `PROVISIONAL`）は
 * **空文字**を入れる。
 *
 * **`BUCKET` 行だけ `accuracy` 列の意味が違う** — 「予想した確率の平均」であり、
 * その帯の的中率は `hitRate` が持つ。列名と中身が一致しない唯一の箇所である
 * （2026-10-08 に本番で的中率が逆に出た原因）。
 */
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

/**
 * `ingestion_logs` の1行。
 *
 * **例外オブジェクトをそのまま入れない**（絶対ルール4）。D1 REST API の URL には
 * アカウントIDと DB ID が含まれ、`/api/v1/health` から漏れる。入れるのは型名と
 * 自前の短いメッセージだけである。
 */
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

/**
 * 選手の実績の集計1行（詳細設計 1.9）。**保存するのは合計で、1試合平均は API が導出する。**
 *
 * **`plusMinus` を持たない。** 旧年度のレスポンスは `PLUSMINUS` のキーを欠くため、
 * 合計すると**欠けた試合が黙って無視される**（母数が「出場した試合数」なのに
 * 分子が「キーがあった試合の合計」になる）。
 */
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

/**
 * クラブの実績の集計1行。**母数を2つ持つ**（詳細設計 1.9）。
 *
 * `games` は勝敗と得点の母数（`team_games` 由来）、`statGames` はボックススコアの
 * 母数（`team_game_stats` 由来）である。**取り込みがスタッツまで届いていない試合が
 * 実在する**ため、1つに畳むと母数が嘘になる。
 */
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
