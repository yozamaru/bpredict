/** ファクト取り込みの入力スキーマ。型だけでなく値域も定義する（詳細設計 3.2）。 */
import { z } from 'zod';

const ID = z.string().min(1).max(64);
const DATE = z.string().regex(/^\d{4}-\d{2}-\d{2}$/);
const TS = z.string().min(1).max(64);
const LEAGUES = ['B1', 'B2', 'B3', 'PREMIER', 'ONE', 'NEXT'] as const;

/** 取り込むのはリーグ戦とチャンピオンシップだけ（要件 5.3）。 */
export const COMPETITIONS = ['REGULAR', 'PLAYOFF'] as const;
/**
 * 中止と延期を分ける（詳細設計 1.3）。**予測の扱いが違う** — どちらも的中率の
 * 母数から外す（`VOID`）が、延期は `rescheduled_to` で延期先を指し、旧行は
 * `POSTPONED` のまま残す。1つに畳むとゴースト試合の予測が母数を汚染する。
 */
export const STATUSES = ['SCHEDULED', 'FINISHED', 'POSTPONED', 'CANCELLED'] as const;

const int = (min: number, max: number) => z.number().int().min(min).max(max);
const zeroOne = z.union([z.literal(0), z.literal(1)]);

/**
 * 試合1件。**この口は2つの経路が共有する**（詳細設計 3.4）。
 *
 * 1. 取り込み（`backfill` / `daily_ingest` のステップ1）— 全列を送る
 * 2. 未実施の試合（ステップ1b）— スコアと `finishedAt` を持たない
 *
 * そのため多くの列が `optional` である。**`venueId` は `preserve` で守る** —
 * ステップ1b は「既に会場IDがある試合は取りに行かない」ため `null` を送り、
 * 保護が無いと**本番で24件が消えた**（2026-10-09。詳細設計 3.4）。
 */
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
    // その試合時点の会場名。`venue_revisions.name` の唯一の入力（詳細設計 1.2）。
    // `venues` の upsert は `name` を更新しないため、ここに残さないと履歴が作れない
    venueNameAtGame: z.string().min(1).max(200).nullable().optional(),
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

/**
 * チーム視点の試合行（詳細設計 3.2）。
 *
 * **`games` への JOIN を消すために存在する。** `home_club_id = ? OR away_club_id = ?`
 * は OR の両辺にインデックスが効かず全件スキャンになり、日程系の特徴量が
 * ほぼすべてこの経路を通る。`competition` をここにも持つのは、区分で絞るたびに
 * JOIN が復活して表の存在理由が失われるのを避けるためである。
 *
 * `result` / `margin` が `null` なら未実施である。
 */
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

/**
 * 試合データから導かれるマスタ（詳細設計 3.4）。
 * backfill が同じ試合レスポンスから抜き出すため、`games` と同じ本文で受ける。
 * `venue_revisions` と `player_seasons` はここでは受けない（U-10 / C03）。
 */
export const venueSchema = z
  .object({
    id: ID,
    name: z.string().min(1).max(200),
    prefecture: z.string().min(1).max(20).nullable().optional(),
    lat: z.number().min(-90).max(90).nullable().optional(),
    lng: z.number().min(-180).max(180).nullable().optional(),
  })
  .strict();

/** 公式の会場ID（`StadiumCD`）→ 内部ID。`club_source_ids` と同じ役割（詳細設計 1.1）。 */
export const venueSourceKeySchema = z
  .object({ sourceCode: ID, venueId: ID })
  .strict();

/**
 * 人物マスタ。試合データの `PlayerID` / `PlayerNameJ` から作る。
 *
 * **`heightCm` は送られてこない**（要件 5.3 — 使う特徴量が無く、個人情報の
 * ページを取りに行かない）。列はあるため、`preserve` で守って**将来入れたときに
 * 取り込みが消さないようにしてある**（詳細設計 3.4）。
 */
export const playerSchema = z
  .object({
    id: ID,
    name: z.string().min(1).max(200),
    heightCm: int(100, 250).nullable().optional(),
  })
  .strict();

/**
 * クラブの年度断面（詳細設計 1.2）。**名称は試合データから作る。**
 *
 * 公式サイトに年度別のクラブ一覧ページが無く、過去シーズンの正式名称を取る経路が
 * `TeamNameJ`（その試合時点の名称）しかない。`703` は 2016-17 で `栃木ブレックス`
 * である。
 *
 * `primaryVenueId` と色は**この経路では送られてこない**（別のジョブが入れる）ため
 * `preserve` で守る（詳細設計 3.4 / 4.11）。
 */
export const clubSeasonSchema = z
  .object({
    clubId: ID,
    seasonId: ID,
    name: z.string().min(1).max(200),
    shortName: z.string().min(1).max(60),
    league: z.enum(LEAGUES),
    primaryVenueId: ID.nullable().optional(),
    colorPrimary: z.string().max(20).nullable().optional(),
    colorSecondary: z.string().max(20).nullable().optional(),
  })
  .strict();

/**
 * `POST /internal/games` の本文。**試合と、試合データから導かれるマスタを一緒に受ける。**
 *
 * 別リクエストに分けないのは、FK の順序と原子性のためである（詳細設計 3.4）。
 * 単一 `batch()` に **`venues` → `venueSourceKeys` → `players` → `clubSeasons`
 * → `games` → `team_games`** の順で入れる — 分けると「試合はあるが会場がない」
 * 状態が残りうる。
 *
 * **テーブル名を引数に取る汎用の口にはしない。** 任意のテーブルへ書ける口を作ると、
 * Zod 検証・認可・`is_final` 保護という関門の意味が失われる。
 */
export const gamesBody = z
  .object({
    // **`games` は省略できる**（詳細設計 3.4）。会場の座標だけを送る経路
    // （4.10 の段5）がこの口を使うためである。v1.55 まで `min(1)` だったため
    // その経路が 400 で通らず、設計の中で矛盾していた。
    // 全部空のリクエストは下の `refine` で拒否する
    games: z.array(gameSchema).optional(),
    teamGames: z.array(teamGameSchema).optional(),
    // FK の順序で入れるため、すべて `games` より前に流す
    venues: z.array(venueSchema).optional(),
    venueSourceKeys: z.array(venueSourceKeySchema).optional(),
    players: z.array(playerSchema).optional(),
    clubSeasons: z.array(clubSeasonSchema).optional(),
  })
  .strict()
  // **1行も書かないリクエストは呼び出し側の誤りである。** `DB.batch([])` を
  // 投げる意味がなく、黙って 200 を返すと「書けたつもり」の事故になる
  .refine(
    (body) =>
      (body.games?.length ?? 0) +
        (body.teamGames?.length ?? 0) +
        (body.venues?.length ?? 0) +
        (body.venueSourceKeys?.length ?? 0) +
        (body.players?.length ?? 0) +
        (body.clubSeasons?.length ?? 0) >
      0,
    { message: '書き込む行が1つもない' },
  );

const countable = int(0, 250).nullable().optional();

/**
 * チームのボックススコア。**公式の合計行（`Category=3`）である**（詳細設計 4.4）。
 *
 * `possessions` の上限を 300 と緩く取ってあるのは、**値域（50〜120）の判定を
 * バッチ側が持つ**ためである — 外れたら `null` にして試合そのものは取り込む
 * （中断・不成立の短い試合が実在する。詳細設計 1.3）。
 */
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

/**
 * 選手のボックススコア。
 *
 * **カウント値はすべて `nullable`。** サイトは値が無いとき 0 ではなく空文字を返し、
 * 旧年度は `plusMinus` のキーそのものが欠ける。**0 で埋めない** — 「記録なし」が
 * 「0回」になると平均の分母に入って集計が狂う（詳細設計 4.4 の正規化3）。
 *
 * `minutes` 0〜60 と `pf` 0〜6 は DDL の CHECK にそろえた（詳細設計 1.3）。
 */
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

/**
 * `POST /internal/stats` の本文。チームと選手を単一 `batch()` で入れる。
 *
 * **1試合を `games` と一体にしない。** この口は `daily_ingest`（結果だけが後から
 * 付く）でも使うため、一体にすると日次側の経路が歪む（詳細設計 4.8）。
 */
export const statsBody = z
  .object({
    teamGameStats: z.array(teamStatSchema).optional(),
    playerGameStats: z.array(playerStatSchema).optional(),
  })
  .strict()
  .refine((b) => b.teamGameStats ?? b.playerGameStats, { message: 'どちらか1つは必要' });

/**
 * 試合ごとの出場登録。`source` が `ESTIMATED` の行が1件でも残る試合は `暫定` である
 * （要件 5.5）。
 */
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

/**
 * 試合日ごとの Elo ほかのスナップショット。**1行はその試合日の「終了時点」の値**
 * （詳細設計 1.4）。
 *
 * **行の意味と絞り込みの不等号は対で決まっている。** 開始前の値を書くと前日の結果が
 * 反映されず、「終了時点」の行を `as_of_date <= 対象試合日` で読むと当日の結果が
 * 混入する（リーク）。
 *
 * `offRating` / `defRating` / `pace` は**集計窓が設計で未定義のため NULL のまま**で
 * ある（詳細設計 2.5）。特徴量は `team_game_stats` から直接集計する。
 */
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
 * 会場の名称・収容人数の履歴（詳細設計 4.9）。
 *
 * **`team_ratings` と同じ「派生テーブルの期間洗い替え」型。** 差分更新をせず、
 * `valid_from BETWEEN ? AND ?` を DELETE してから INSERT する。
 */
export const venueRevisionSchema = z
  .object({
    venueId: ID,
    validFrom: DATE,
    validTo: DATE,
    name: z.string().min(1).max(200),
    // 手入力 CSV に行がない会場は NULL。設計はこれを許容する（詳細設計 1.2）
    capacity: int(0, 200000).nullable().optional(),
  })
  .strict()
  .refine((r) => r.validFrom <= r.validTo, { message: 'validFrom が validTo より後' });

export const venueRevisionsBody = z
  .object({
    fromDate: DATE,
    toDate: DATE,
    revisions: z.array(venueRevisionSchema),
  })
  .strict()
  .refine((b) => b.fromDate <= b.toDate, { message: 'fromDate が toDate より後' })
  .refine(
    (b) => b.revisions.every((r) => b.fromDate <= r.validFrom && r.validFrom <= b.toDate),
    { message: 'revisions に期間外の validFrom がある' },
  )
  .refine(
    (b) =>
      new Set(b.revisions.map((r) => `${r.venueId}\u0000${r.validFrom}`)).size ===
      b.revisions.length,
    { message: '(venueId, validFrom) が重複している' },
  );

/**
 * Elo は差分更新せず対象期間を再計算して洗い替える（CLAUDE.md 冪等性）。
 * 期間の DELETE と INSERT は単一 `batch()` に入れる。
 *
 * **同じ `as_of_date` を2リクエストに分けない。** 期間 DELETE が前のリクエストで
 * 入れた行を消す（詳細設計 2.5）。1日の行数は最大26で、上限480に対して十分小さい。
 */
export const ratingsBody = z
  .object({ fromDate: DATE, toDate: DATE, ratings: z.array(ratingSchema) })
  .strict()
  .refine((b) => b.fromDate <= b.toDate, { message: 'fromDate が toDate より後' })
  .refine((b) => b.ratings.every((r) => b.fromDate <= r.asOfDate && r.asOfDate <= b.toDate), {
    message: 'ratings に期間外の asOfDate がある',
  });
