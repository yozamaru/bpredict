// 生成した HTML の内部リンクが、すべて `out/` に実体を持つことを検査する。
//
// **「リンクはあるのに開けない」を機械で捕まえる。** 静的出力では、リンク先を
// `generateStaticParams` に足し忘れると 404 になる。目視では気づけない —
// 13試合の日のカードと予測履歴で、実際に18件が壊れていた（2026-09-26）。
//
// 受け入れ基準 A-07（免責・出典が全ページから到達可能）の一部もここで守る。
// **外部リンクは検査しない**（ネットワークに出ない。CI を外部に依存させない）。
//
// 依存を増やさない（Node の標準モジュールだけ）。
import { readdir, readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { join, resolve } from 'node:path';

const OUT = resolve(import.meta.dirname, '..', 'out');

async function htmlFiles(dir) {
  const found = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) found.push(...(await htmlFiles(path)));
    else if (entry.name.endsWith('.html')) found.push(path);
  }
  return found;
}

/** `/games/demo-1/` → `out/games/demo-1/index.html`。末尾スラッシュ運用（next.config.ts） */
function resolveTarget(href) {
  const clean = href.replace(/[#?].*$/, '');
  if (clean === '/') return join(OUT, 'index.html');
  const base = join(OUT, clean.replace(/^\/|\/$/g, ''));
  return existsSync(base) ? base : join(base, 'index.html');
}

if (!existsSync(OUT)) {
  console.error('out/ がない。先に `npm run build` を実行する');
  process.exit(1);
}

const pages = await htmlFiles(OUT);
const broken = new Map();
let total = 0;

for (const page of pages) {
  const body = await readFile(page, 'utf8');
  for (const [, href] of body.matchAll(/href="(\/[^"]*)"/g)) {
    total += 1;
    if (existsSync(resolveTarget(href))) continue;
    const from = page.slice(OUT.length) || '/';
    if (!broken.has(href)) broken.set(href, new Set());
    broken.get(href).add(from);
  }
}

console.log(`内部リンク ${total} 箇所 / ページ ${pages.length} 件を検査した`);

if (broken.size > 0) {
  for (const [href, sources] of [...broken].sort()) {
    console.error(`NG ${href}  ← ${[...sources].sort().join(', ')}`);
  }
  console.error(`\nリンク先のないリンクが ${broken.size} 種類ある。`);
  console.error('静的出力では `generateStaticParams` への足し忘れが 404 になる。');
  process.exit(1);
}

console.log('OK  すべての内部リンクに実体がある。');
