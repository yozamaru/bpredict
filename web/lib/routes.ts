// 静的生成する試合ID（要件 8.2）。**ビルド時にだけ読む。**
//
// **出典はコミット済みの静的JSON である**（`web/public/data/`）。バッチが
// 当日＋7日ぶんの一覧を書き出してコミットしており（詳細設計 3.7）、
// **ビルド時に読める試合IDの唯一の источник**がこれである。
//
// **承知しておく — 窓の外の試合には詳細ページが無い。** 要件 8.2 は「直近3シーズン」
// の試合を静的生成すると定めるが、**試合IDの一覧をビルド時に得る経路が設計に無い**
// （`seasons.csv` は日付しか持たず、試合IDはスナップショット = parquet にしかない）。
// さらに**試合IDは毎日増える**ため、静的出力では**デプロイのたびに生成し直す**必要が
// ある。この2点は運営者の判断を待つ（`docs/STATUS.md`）。

import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';

const DATA = join(process.cwd(), 'public', 'data');

function idsOf(path: string): string[] {
  try {
    const body: unknown = JSON.parse(readFileSync(path, 'utf8'));
    if (typeof body !== 'object' || body === null || !('data' in body)) return [];
    const data = (body).data;
    if (typeof data !== 'object' || data === null || !('games' in data)) return [];
    const games = (data).games;
    if (!Array.isArray(games)) return [];
    return games
      .map((game) =>
        typeof game === 'object' && game !== null && 'gameId' in game
          ? String((game as { gameId: unknown }).gameId)
          : '',
      )
      .filter((id) => id !== '');
  } catch {
    // **落とさない。** 書き出し前（初回ビルド）は一覧が存在しない
    return [];
  }
}

/** 窓の中の試合ID（当日＋7日）。**重複を除き、順序を固定する。** */
export function staticGameIds(): string[] {
  const found: string[] = [];
  const today = join(DATA, 'today.json');
  if (existsSync(today)) found.push(...idsOf(today));
  const scheduleDir = join(DATA, 'schedule');
  if (existsSync(scheduleDir)) {
    for (const name of readdirSync(scheduleDir).sort()) {
      if (name.endsWith('.json')) found.push(...idsOf(join(scheduleDir, name)));
    }
  }
  return [...new Set(found)].sort();
}
