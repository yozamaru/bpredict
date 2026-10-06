// 出典表記と免責が**全ページ**に出ていることを検査する（要件 4.5.5 / F-12 / A-07）。
//
// **`check-links.mjs` では足りない。** あちらは「`/about` へのリンクが開けるか」を見るが、
// 要件 4.5.5 は**出典の確定文言を全ページ共通フッターに固定表示する**ことを求めている。
// リンクが生きていても、文言がフッターから落ちれば要件に反する。
//
// **目視では気づけない。** フッターは全ページの末尾にあり、1ページ見て確かめた気に
// なりやすい。レイアウトから `<Footer />` が外れても画面は壊れない。
//
// **「使用してはならない表記」も見る。** 要件 4.5.5 は「Powered by B.LEAGUE」等を
// 名指しで禁じており、これは不正競争防止法・商標法に関わる（要件 4.5.7 / R-12）。
// **一度入ると気づかないまま配信され続ける**種類の誤りである。
//
// 依存を増やさない（Node の標準モジュールだけ）。
import { readFileSync } from 'node:fs';
import { readdir, readFile } from 'node:fs/promises';
import { join, relative, resolve } from 'node:path';

const OUT = resolve(import.meta.dirname, '..', 'out');

/**
 * フッターに必ず出ていなければならない断片（要件 4.5.5 の確定文言）。
 *
 * **文全体で照合しない。** JSX は `<a>` や改行で文を分けるため、タグを除いた
 * あとの空白の畳み方に依存してしまう。**言い換えでは通らない長さ**の断片を選ぶ。
 */
const REQUIRED = [
  'データ出典:B.LEAGUE公式サイト',
  '本サイトが独自に集計・加工したものです',
  '個人が運営する非公式サービスです',
  '各クラブへお問い合わせいただくことはご遠慮ください',
  // **版**（要件 F-12 / 8.2）。下で `lib/version.ts` から読んで差し込む
];

/**
 * 版を**唯一の出典から読む**（要件 8.2）。ここに文字列を書き写すと、版を
 * 上げたときに2か所を直すことになり、**片方が古くなっても検査は通る**。
 *
 * `import` しないのは、このスクリプトが `.mjs` で TypeScript の読み込みに
 * 依存したくないためである（**依存も実行時の前提も増やさない**）。
 */
function siteVersion() {
  const file = resolve(import.meta.dirname, '..', 'lib', 'version.ts');
  const source = readFileSync(file, 'utf8');
  const found = /SITE_VERSION\s*=\s*'([^']+)'/.exec(source);
  if (!found) {
    console.error('lib/version.ts から SITE_VERSION を読めない');
    process.exit(1);
  }
  return found[1];
}

// 空白を畳んでから探すため、ここでも畳む（`ver. 1.0.0 beta` → `ver.1.0.0beta`）
REQUIRED.push(siteVersion().replace(/\s+/g, ''));

/**
 * 出てはならない表記（要件 4.5.5）。**公式との関係を示唆する表記**である。
 *
 * 空白を畳んでから探すため、ここも空白を入れずに書く。
 */
const FORBIDDEN = [
  'PoweredbyB.LEAGUE',
  'B.LEAGUE公式データ',
  'B.LEAGUE提供',
];

async function htmlFiles(dir) {
  const found = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) found.push(...(await htmlFiles(path)));
    else if (entry.name.endsWith('.html')) found.push(path);
  }
  return found;
}

/** タグを除いて空白を畳む。**JSX の改行に依存しないため。** */
function visibleText(html) {
  return html
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, ' ')
    .replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi, ' ')
    .replace(/<[^>]+>/g, ' ')
    .replace(/\s+/g, '');
}

const pages = await htmlFiles(OUT);
if (pages.length === 0) {
  console.error('out/ に HTML がない。先に `npm run build` を実行する');
  process.exit(1);
}

const missing = [];
const forbidden = [];
for (const path of pages) {
  const text = visibleText(await readFile(path, 'utf8'));
  const name = relative(OUT, path);
  for (const probe of REQUIRED) {
    if (!text.includes(probe)) missing.push(`${name}: ${probe}`);
  }
  for (const probe of FORBIDDEN) {
    if (text.includes(probe)) forbidden.push(`${name}: ${probe}`);
  }
}

if (missing.length > 0 || forbidden.length > 0) {
  // **先頭の数件だけ出す。** 4,900ページあるため、全件出すとログが読めない
  for (const line of missing.slice(0, 10)) console.error(`出典表記が無い  ${line}`);
  if (missing.length > 10) console.error(`  ほか ${missing.length - 10} 件`);
  for (const line of forbidden.slice(0, 10)) console.error(`禁じた表記がある  ${line}`);
  if (forbidden.length > 10) console.error(`  ほか ${forbidden.length - 10} 件`);
  console.error(
    `\n要件 4.5.5 は出典の確定文言を全ページ共通フッターに固定表示することを求める。`
    + `\n短縮・言い換えをしない（${pages.length}ページを検査した）。`,
  );
  process.exit(1);
}

console.log(
  `OK  ${pages.length}ページすべてに出典表記と非公式表明と版（${siteVersion()}）があり、`
  + `禁じた表記（${FORBIDDEN.length}種）はどこにも無い。`,
);
