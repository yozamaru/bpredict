// 静的生成する選手ID（要件 8.2 / F-15）。**ビルド時にだけ読む。**
//
// **出典は `web/data/players.csv`** — 集計ジョブ（`batch.jobs.summarize_stats`）が
// 書き、`daily_ingest` がコミットする生成物である（詳細設計 4.14）。
// **手で編集しない**（CLAUDE.md の生成物の表）。
//
// **`players.parquet` を読まない。** JS から parquet は読めず、CSV は同じ値から
// 書き出したものである（`web/lib/seasons.ts` の注記と同じ）。
//
// **承知しておく — 新しい選手のページは次のデプロイまで出ない。** 静的出力であり、
// CSV が更新されてもビルドし直さなければルートが増えない（`lib/routes.ts` の
// 試合IDと同じ制約である）。

import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';

const CSV = join(process.cwd(), 'data', 'players.csv');

/**
 * 直近3シーズンに出場した選手のID（昇順）。
 *
 * **CSV が無ければ空を返す。** 初回ビルド（集計ジョブを一度も流していない状態）で
 * 落とさない — 選手ページが生成されないだけで、他の画面は出る。
 */
export function staticPlayerIds(): string[] {
  if (!existsSync(CSV)) return [];
  const [header, ...lines] = readFileSync(CSV, 'utf8').trim().split('\n');
  const at = (header ?? '').split(',').indexOf('player_id');
  if (at < 0) throw new Error('players.csv に player_id 列がない');
  return lines
    .map((line) => line.split(',')[at] ?? '')
    .filter((id) => id !== '');
}
