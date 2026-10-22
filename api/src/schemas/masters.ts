/**
 * `POST /internal/masters` の入力スキーマ。
 *
 * **型だけでなく値域も定義する**（詳細設計 3.2）。DB の CHECK 制約と同じ条件を
 * 境界で弾き、D1 に触らずに 400 を返す。
 *
 * **テーブル名を引数に取る汎用エンドポイントにしない。** 名前付きの配列にし、
 * 配列ごとに個別のスキーマを持つ（詳細設計 3.4）。
 */
import { z } from 'zod';

const DATE = z.string().regex(/^\d{4}-\d{2}-\d{2}$/, 'YYYY-MM-DD 形式であること');
const ID = z.string().min(1).max(64);

/** `seasons.league` の CHECK 制約と同じ集合。 */
export const LEAGUES = ['B1', 'B2', 'B3', 'PREMIER', 'ONE', 'NEXT'] as const;

/**
 * シーズン1件。
 *
 * **`id` にリーグを含める**（`2026-27-PREMIER`。詳細設計 1.1）。`2026-27` 単独では
 * 同一シーズンの PREMIER と ONE を同時に持てず、B.ONE 拡張時に主キー変更
 * ＝全ファクトテーブルの FK 移行になる。
 *
 * **開始日・終了日は「当季の9月1日〜翌年6月30日」に固定する。** 範囲は取り込む
 * 試合日の上位集合でよく、**狭いと実在する試合日が404になる**（日程一覧からの導出は
 * CS を取りこぼし、実際に20日ぶんが範囲外だった）。
 */
export const seasonSchema = z
  .object({
    id: ID,
    label: z.string().min(1).max(32),
    league: z.enum(LEAGUES),
    startDate: DATE,
    endDate: DATE,
  })
  .strict()
  .refine((s) => s.startDate <= s.endDate, {
    message: 'startDate が endDate より後になっている',
  });

/**
 * 恒久的なクラブ。**名称変更・リーグ移動があっても不変である。**
 *
 * **`slug` は手で決め、改称でも変えない**（詳細設計 1.1）。英語名からの機械導出は
 * 2016-17 で `TeamNameE` が空だった実例があり、改称のたびに URL が変わる。
 * 表示名は `club_seasons.name` が持つ。
 */
export const clubSchema = z
  .object({
    id: ID,
    // 詳細設計 3.2 の `slug` 制約。URL の識別子であり、恒久である
    slug: z.string().regex(/^[a-z0-9-]{1,40}$/, '^[a-z0-9-]{1,40}$ に合わないslug'),
    name: z.string().min(1).max(128),
  })
  .strict();

/**
 * 公式サイトのチームID → `club_id` の対応（詳細設計 1.2）。
 *
 * **取り込み時にここで解決してから保存する。** 旧IDのまま保存すると
 * `team_ratings.club_id` が分断され、**旧B1の Elo を新リーグへ引き継げず**
 * コールドスタート対策が実装不能になる。
 */
export const clubSourceIdSchema = z
  .object({
    sourceId: ID,
    clubId: ID,
    validFrom: DATE,
    validTo: DATE,
    note: z.string().max(256).nullable().optional(),
  })
  .strict();

/**
 * 3つの配列はいずれも省略可。与えられたものだけを処理する。
 *
 * `.strict()` にすることで、`{"table": "games", "rows": [...]}` のような
 * 「任意のテーブルへ書こうとする形」を 400 で弾く。
 */
export const mastersSchema = z
  .object({
    seasons: z.array(seasonSchema).optional(),
    clubs: z.array(clubSchema).optional(),
    clubSourceIds: z.array(clubSourceIdSchema).optional(),
  })
  .strict()
  .refine((b) => b.seasons ?? b.clubs ?? b.clubSourceIds, {
    message: 'seasons / clubs / clubSourceIds のいずれか1つは必要',
  });

export type MastersBody = z.infer<typeof mastersSchema>;
