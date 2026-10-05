// 静的生成の範囲（要件 8.2）。**ビルド時にだけ読む。**
//
// **出典は `db/seeds/master/seasons.csv` である**（詳細設計 1.1）。シーズンの
// 開始日・終了日は「当季の9月1日〜翌年6月30日」に固定してあり、**この2列が
// URL 空間の有限化に使われる**（範囲外の日付は 404 で打ち切る）。
//
// **`seasons.parquet` を読まない。** JS から parquet は読めず、CSV は同じ値の
// 正本である（スナップショットはあの CSV から作られる）。

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

/** 静的生成するシーズン数（要件 8.2。**直近3シーズン**で約76%）。 */
export const STATIC_SEASONS = 3;

type Season = { id: string; label: string; startDate: string; endDate: string };

function load(): Season[] {
  // `web/` から見た位置。**ビルド時にだけ呼ぶ**（クライアントには入らない）
  const path = join(process.cwd(), '..', 'db', 'seeds', 'master', 'seasons.csv');
  const [header, ...lines] = readFileSync(path, 'utf8').trim().split('\n');
  const columns = (header ?? '').split(',');
  const index = (name: string) => {
    const at = columns.indexOf(name);
    if (at < 0) throw new Error(`seasons.csv に ${name} 列がない`);
    return at;
  };
  const [id, label, start, end] = [
    index('id'), index('label'), index('start_date'), index('end_date'),
  ];
  return lines
    .filter((line) => line.trim() !== '')
    .map((line) => {
      const cells = line.split(',');
      return {
        id: cells[id] ?? '',
        label: cells[label] ?? '',
        startDate: cells[start] ?? '',
        endDate: cells[end] ?? '',
      };
    });
}

/**
 * 静的生成する日付（直近3シーズン分）。
 *
 * **`start_date` の降順で3つ取る。** `id` の降順で並べない — 文字列比較では
 * `'2026-27' > '2025-26'` は成り立つが、将来の採番で崩れる（詳細設計 3.3 の
 * `latestSeasonId` と同じ理由）。
 */
export function staticDates(): string[] {
  const seasons = load()
    .sort((a, b) => (a.startDate < b.startDate ? 1 : -1))
    .slice(0, STATIC_SEASONS);
  const dates: string[] = [];
  for (const season of seasons) {
    for (
      let at = Date.parse(`${season.startDate}T00:00:00Z`);
      at <= Date.parse(`${season.endDate}T00:00:00Z`);
      at += 86_400_000
    ) {
      dates.push(new Date(at).toISOString().slice(0, 10));
    }
  }
  // **重複を除く。** シーズンの範囲は上位集合であり、隣り合う季が重なりうる
  return [...new Set(dates)].sort();
}
