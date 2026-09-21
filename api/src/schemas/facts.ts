/** ファクト取り込みの入力スキーマ。型だけでなく値域も定義する（詳細設計 3.2）。 */
import { z } from 'zod';

const ID = z.string().min(1).max(64);
const DATE = z.string().regex(/^\d{4}-\d{2}-\d{2}$/);
const TS = z.string().min(1).max(64);
const LEAGUES = ['B1', 'B2', 'B3', 'PREMIER', 'ONE', 'NEXT'] as const;

/** 取り込むのはリーグ戦とチャンピオンシップだけ（要件 5.3）。 */
export const COMPETITIONS = ['REGULAR', 'PLAYOFF'] as const;
export const STATUSES = ['SCHEDULED', 'FINISHED', 'POSTPONED', 'CANCELLED'] as const;

const int = (min: number, max: number) => z.number().int().min(min).max(max);
const zeroOne = z.union([z.literal(0), z.literal(1)]);

export const gameSchema = z
  .object({
    id: ID,
    seasonId: ID,
    league: z.enum(LEAGUES),
    competition: z.enum(COMPETITIONS),
    gameDate: DATE,
    tipoffAt: TS,
    finishedAt: TS.nullable().optional(),
    finishedAtIsEstimated: zeroOne.optional(),
    homeClubId: ID,
    awayClubId: ID,
    venueId: ID.nullable().optional(),
    isPrimaryVenue: zeroOne.optional(),
    seriesGameNo: int(1, 10).nullable().optional(),
    status: z.enum(STATUSES),
    rescheduledTo: ID.nullable().optional(),
    homeScore: int(0, 250).nullable().optional(),
    awayScore: int(0, 250).nullable().optional(),
    attendance: int(0, 200000).nullable().optional(),
    // NULL = 判定不能。1 のときだけ Elo のホームアドバンテージを 0 にする（詳細設計 2.5）
    spectatorRestricted: zeroOne.nullable().optional(),
    resultRevision: int(0, 1000).optional(),
    sourceUrl: z.string().max(512).nullable().optional(),
    fetchedAt: TS.nullable().optional(),
  })
  .strict()
  .refine((g) => g.homeClubId !== g.awayClubId, { message: '同一クラブ同士の試合は入らない' });

export const teamGameSchema = z
  .object({
    gameId: ID,
    clubId: ID,
    opponentId: ID,
    seasonId: ID,
    gameDate: DATE,
    finishedAt: TS.nullable().optional(),
    isHome: zeroOne,
    competition: z.enum(COMPETITIONS),
    result: zeroOne.nullable().optional(),
    margin: int(-250, 250).nullable().optional(),
  })
  .strict();

export const gamesBody = z
  .object({ games: z.array(gameSchema).min(1), teamGames: z.array(teamGameSchema).optional() })
  .strict();

const countable = int(0, 250).nullable().optional();

export const teamStatSchema = z
  .object({
    gameId: ID,
    clubId: ID,
    gameDate: DATE,
    isHome: zeroOne,
    pts: countable,
    fg2m: countable, fg2a: countable,
    fg3m: countable, fg3a: countable,
    ftm: countable, fta: countable,
    oreb: countable, dreb: countable,
    ast: countable, tov: countable, stl: countable, blk: countable,
    pf: countable, fd: countable,
    possessions: z.number().min(0).max(300).nullable().optional(),
    fetchedAt: TS,
  })
  .strict();

export const playerStatSchema = z
  .object({
    gameId: ID,
    playerId: ID,
    clubId: ID,
    gameDate: DATE,
    started: zeroOne.nullable().optional(),
    minutes: z.number().min(0).max(60).nullable().optional(),
    fg2m: countable, fg2a: countable,
    fg3m: countable, fg3a: countable,
    ftm: countable, fta: countable,
    oreb: countable, dreb: countable,
    ast: countable, tov: countable, stl: countable, blk: countable,
    pf: int(0, 6).nullable().optional(),
    fd: countable,
    plusMinus: int(-250, 250).nullable().optional(),
    pts: countable,
    fetchedAt: TS,
  })
  .strict();

export const statsBody = z
  .object({
    teamGameStats: z.array(teamStatSchema).optional(),
    playerGameStats: z.array(playerStatSchema).optional(),
  })
  .strict()
  .refine((b) => b.teamGameStats ?? b.playerGameStats, { message: 'どちらか1つは必要' });

export const entrySchema = z
  .object({
    gameId: ID,
    playerId: ID,
    status: z.enum(['ENTRY', 'OUT', 'UNKNOWN']),
    source: z.enum(['OFFICIAL', 'ESTIMATED']),
    confidence: z.number().min(0).max(1).nullable().optional(),
    fetchedAt: TS,
  })
  .strict();

/**
 * エントリーは**取得ごとに当該試合の全行を洗い替える**（要件 5.5）。
 * 追加のみの upsert では推定行が残り `暫定` が永久に解除されない。
 */
export const entriesBody = z
  .object({ gameId: ID, entries: z.array(entrySchema) })
  .strict()
  .refine((b) => b.entries.every((e) => e.gameId === b.gameId), {
    message: 'entries の gameId が本文の gameId と一致していない',
  });

export const ratingSchema = z
  .object({
    clubId: ID,
    asOfDate: DATE,
    seasonId: ID,
    elo: z.number().min(0).max(4000),
    offRating: z.number().nullable().optional(),
    defRating: z.number().nullable().optional(),
    pace: z.number().nullable().optional(),
    gamesPlayed: int(0, 2000),
  })
  .strict();

/**
 * Elo は差分更新せず対象期間を再計算して洗い替える（CLAUDE.md 冪等性）。
 * 期間の DELETE と INSERT は単一 `batch()` に入れる。
 */
export const ratingsBody = z
  .object({ fromDate: DATE, toDate: DATE, ratings: z.array(ratingSchema) })
  .strict()
  .refine((b) => b.fromDate <= b.toDate, { message: 'fromDate が toDate より後' })
  .refine((b) => b.ratings.every((r) => b.fromDate <= r.asOfDate && r.asOfDate <= b.toDate), {
    message: 'ratings に期間外の asOfDate がある',
  });
