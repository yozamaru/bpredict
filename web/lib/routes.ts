// 静的生成する試合ID（要件 8.2）。**ビルド時にだけ読む。**
//
// **出典は `public/data/games/index.json`**（予測を出した試合IDの索引）。
// バッチが書き出しのたびに**溜めて**コミットする（詳細設計 3.7 / 5.6）。
// `web/data/players.csv` と同じ「ビルド時にだけ読む committed な一覧」である。
//
// **窓（当日＋7日）を出典にしてはならない。** 旧版は `today.json` と
// `schedule/*.json` から拾っていたが、**窓から出た試合の詳細ファイルは削除される**
// ため（3.7）、昨日の試合を押すと 404 になった — 2026-10-08 に運営者が実際に踏み、
// 「予想と結果の対比が確認できるページが無いとこのサービスの意味が無い」と
// 指摘された。**過去の試合こそ対比の置き場である。**
//
// **新しい試合のページは次のデプロイまで出ない。** 静的出力であるため、索引が
// 更新されてもビルドし直さなければルートが増えない（`players.csv` と同じ制約）。

import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';

const INDEX = join(process.cwd(), 'public', 'data', 'games', 'index.json');

/**
 * 予測を出した試合ID。**重複を除き、順序を固定する。**
 *
 * 索引が無い・壊れているときは空を返す（**ビルドを落とさない**）。
 * **ただしそのときページが1枚も作られず、一覧のリンクは 404 になる** —
 * 一覧（`GameBoardList`）はリンクを無条件に張るためである。**`npm run test:games`
 * が、一覧に出ている試合がすべて索引にあることを検査する**（静かに壊れさせない）。
 */
export function staticGameIds(): string[] {
  if (!existsSync(INDEX)) return [];
  try {
    const body: unknown = JSON.parse(readFileSync(INDEX, 'utf8'));
    if (typeof body !== 'object' || body === null || !('gameIds' in body)) return [];
    const ids: unknown = body.gameIds;
    if (!Array.isArray(ids)) return [];
    return [...new Set(ids.filter((id): id is string => typeof id === 'string' && id !== ''))].sort();
  } catch {
    return [];
  }
}
