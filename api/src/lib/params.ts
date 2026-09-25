/**
 * 公開エンドポイントの入力検証（詳細設計 3.2）。
 *
 * **すべてのパラメータを境界で検証し、不一致は D1 にもキャッシュにも触れず 400 で返す。**
 * 範囲外の日付を弾かないと URL 空間が無限になり、クローラの総当たりで無料枠が枯渇する
 * （要件 4.2）。
 */

/** `YYYY-MM-DD`。**実在日付であること**まで見る（`2026-02-30` を通さない） */
const DATE = /^(\d{4})-(\d{2})-(\d{2})$/;

/**
 * 構文の検査に使う粗い絶対範囲。**シーズン範囲の判定ではない。**
 *
 * シーズン範囲は `seasons` が正本であり、D1 に1クエリ投げて判定する
 * （`isWithinSeason`）。その前段で「そもそも扱う気のない年」を落とすのが
 * ここの役目で、**D1 に触れる回数を増やさないため**にある。
 * 下限は最初のシーズン（2016-17）の前年、上限は十分に先の年とする。
 */
const EARLIEST_YEAR = 2016;
const LATEST_YEAR = 2100;

export function parseDate(value: string | undefined): string | null {
  if (value === undefined) return null;
  const matched = DATE.exec(value);
  if (!matched) return null;
  const [, y, m, d] = matched;
  const year = Number(y);
  const month = Number(m);
  const day = Number(d);
  if (year < EARLIEST_YEAR || year > LATEST_YEAR) return null;
  // **実在日付の確認。** `Date` は 2026-02-30 を 3月2日として受けるため、
  // 組み立て直して一致を見る
  const date = new Date(Date.UTC(year, month - 1, day));
  if (
    date.getUTCFullYear() !== year ||
    date.getUTCMonth() !== month - 1 ||
    date.getUTCDate() !== day
  ) {
    return null;
  }
  return value;
}

/** `^[a-z0-9-]{1,40}$`（詳細設計 3.2）。`clubs.slug` の形式と同じ */
const SLUG = /^[a-z0-9-]{1,40}$/;

export function parseSlug(value: string | undefined): string | null {
  if (value === undefined || !SLUG.test(value)) return null;
  return value;
}

/** 既定20、最大100。**範囲外はクランプする**（詳細設計 3.2） */
export function parseLimit(value: string | undefined, fallback = 20, max = 100): number {
  if (value === undefined) return fallback;
  if (!/^\d{1,4}$/.test(value)) return fallback;
  const n = Number(value);
  if (n < 1) return 1;
  return Math.min(n, max);
}

/**
 * その日付がいずれかのシーズンの期間内にあるか。**`seasons` が正本である。**
 *
 * `seasons.start_date` / `end_date` は日程一覧から1回だけ導出して CSV に固定した値で、
 * **実際の試合日を必ず含む上位集合**である（詳細設計 1.1）。ここで外れた日付は
 * 404 にし、試合の検索まで進めない。
 */
export async function isWithinSeason(db: D1Database, date: string): Promise<boolean> {
  const row = await db
    .prepare('SELECT 1 AS ok FROM seasons WHERE ? BETWEEN start_date AND end_date LIMIT 1')
    .bind(date)
    .first<{ ok: number }>();
  return row !== null;
}

/**
 * 「当季」のシーズンID。**最も新しく始まったシーズンを返す。**
 *
 * **「いま」を見ない。** 時計を見る実装にすると (a) 同じURLの応答が日をまたいで変わり
 * キャッシュと噛み合わず、(b) テストが実行日に依存して将来必ず壊れる。
 * `seasons` には開幕前から当季の行が入っている（`seed_master` が投入する）ため、
 * **最新の `start_date` が当季（またはこれから始まる季）を指す**。
 *
 * オフシーズンでも最後のシーズンを返す。クラブ一覧が空になるのを避けるためで、
 * 試合や予測が無いことは画面が空状態で扱う（要件 8.5）。
 */
export async function latestSeasonId(db: D1Database): Promise<string | null> {
  const row = await db
    .prepare('SELECT id FROM seasons ORDER BY start_date DESC, id DESC LIMIT 1')
    .first<{ id: string }>();
  return row?.id ?? null;
}
