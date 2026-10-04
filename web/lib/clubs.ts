// 静的生成するクラブの slug（要件 8.2）。**ビルド時にだけ読む。**
//
// **出典は `db/seeds/master/clubs.csv` である**（詳細設計 1.1）。slug は運営者が
// 決めた恒久の識別子で、**改称があっても変えない** — 表示名は
// `club_seasons.name` が持つ。

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

/** 全クラブの slug（出現順を保つ）。 */
export function staticSlugs(): string[] {
  const path = join(process.cwd(), '..', 'db', 'seeds', 'master', 'clubs.csv');
  const [header, ...lines] = readFileSync(path, 'utf8').trim().split('\n');
  const at = (header ?? '').split(',').indexOf('slug');
  if (at < 0) throw new Error('clubs.csv に slug 列がない');
  return lines
    .map((line) => line.split(',')[at] ?? '')
    .filter((slug) => slug !== '');
}
