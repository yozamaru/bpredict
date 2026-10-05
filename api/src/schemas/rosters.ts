/**
 * 登録選手一覧の入力スキーマ（詳細設計 1.2 / 3.4 / 4.13）。
 *
 * 出典は `/roster/?year&club` で、**試合データではない**。そのため
 * `POST /internal/games` とは別の口で受ける（3.4）。
 */
import { z } from 'zod';

import { playerSchema } from './facts';

const ID = z.string().min(1).max(64);

/** `player_seasons.position` の CHECK 制約（詳細設計 1.2）。 */
export const POSITIONS = ['PG', 'SG', 'SF', 'PF', 'C'] as const;

/** `player_seasons.roster_type` の CHECK 制約。**いまは送られてこない。** */
export const ROSTER_TYPES = ['JP', 'NATURALIZED', 'ASIA', 'FOREIGN'] as const;

export const playerSeasonSchema = z
  .object({
    playerId: ID,
    seasonId: ID,
    clubId: ID,
    // 背番号は TEXT。`0` が実在するため数値にしない
    number: z.string().min(1).max(10).nullable().optional(),
    // **複数値は送られてこない。** 先頭だけを採るのは parser の責務であり
    // （運営者の判断。詳細設計 1.2）、この口は正規化済みの5値だけを受ける
    position: z.enum(POSITIONS).nullable().optional(),
    // **出典が無いため、いまは送られてこない**（要件 5.3）。列は受けられるように
    // しておく — 将来出典が見つかったときに口から直す必要がない
    rosterType: z.enum(ROSTER_TYPES).nullable().optional(),
    joinedOn: z.string().regex(/^\d{4}-\d{2}-\d{2}$/).nullable().optional(),
    leftOn: z.string().regex(/^\d{4}-\d{2}-\d{2}$/).nullable().optional(),
  })
  .strict();

export const rostersBody = z
  .object({
    players: z.array(playerSchema).optional(),
    playerSeasons: z.array(playerSeasonSchema).optional(),
  })
  .strict()
  // **1つも書かないリクエストは呼び出し側の誤りである**（3.4 の `games` と同じ）
  .refine(
    (b) => (b.players?.length ?? 0) + (b.playerSeasons?.length ?? 0) > 0,
    { message: '選手または所属断面を1件以上送る' },
  );
