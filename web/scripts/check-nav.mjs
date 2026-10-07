// 生成した HTML で、**タブの現在地が自分のページを指している**ことを検査する。
//
// `layout.tsx` が `<Header />` を引数なしで描いており、既定値 `'/'` のまま
// 全ページが「今日の予測」を現在地として出していた（2026-09-26 に実機で発覚）。
// 下線が動かないだけでなく、**`aria-current` も誤っていた** — 読み上げでは
// 現在地がまったく伝わらない（詳細設計 5.2 / 要件 8.6）。
//
// E2E は工程15であり、それまでこの種の誤りを捕まえるものが何もなかった。
// **静的出力なら、生成物を読めば機械で判定できる。**
//
// 依存を増やさない（Node の標準モジュールだけ）。
import { readFile } from 'node:fs/promises';
import { existsSync, readFileSync } from 'node:fs';
import { join, resolve } from 'node:path';

const OUT = resolve(import.meta.dirname, '..', 'out');

/**
 * タブの定義を**`components/ui/Tabs.tsx` から読む**。
 *
 * **書き写さない。** 旧版はここに配列を持ち「対応させる」と注意書きしていたが、
 * **タブを足しても検査が追随しない**（2026-10-07 に「チーム」を足して気づいた）。
 * 増えたタブは検査されず、`aria-current` が誤っていても通る。
 */
function tabs() {
  const file = resolve(import.meta.dirname, '..', 'components', 'ui', 'Tabs.tsx');
  const source = readFileSync(file, 'utf8');
  const block = /const TABS = \[([\s\S]*?)\] as const;/.exec(source);
  if (block === null) {
    console.error('Tabs.tsx から TABS を読めない');
    process.exit(1);
  }
  const found = [...block[1].matchAll(/href:\s*'([^']+)'/g)].map((m) => m[1]);
  if (found.length === 0) {
    console.error('Tabs.tsx の TABS に href が無い');
    process.exit(1);
  }
  return found;
}

const TABS = tabs();

function pageOf(href) {
  return href === '/' ? join(OUT, 'index.html') : join(OUT, href, 'index.html');
}

/** `aria-current="page"` が付いた `<a>` の href を集める */
function currentHrefs(html) {
  const found = [];
  for (const anchor of html.match(/<a\b[^>]*>/g) ?? []) {
    if (!anchor.includes('aria-current="page"')) continue;
    const href = anchor.match(/href="([^"]*)"/);
    found.push(href === null ? '' : href[1]);
  }
  return found;
}

const problems = [];
for (const href of TABS) {
  const path = pageOf(href);
  if (!existsSync(path)) {
    problems.push(`${href}: ページが生成されていない`);
    continue;
  }
  const marked = currentHrefs(await readFile(path, 'utf8'));
  if (marked.length !== 1) {
    problems.push(`${href}: aria-current="page" が ${marked.length} 箇所（1箇所であること）`);
    continue;
  }
  if (marked[0] !== href) {
    problems.push(`${href}: 現在地が ${marked[0]} を指している`);
  }
}

console.log(`タブの現在地を ${TABS.length} ページで検査した`);

if (problems.length > 0) {
  for (const problem of problems) console.error(`NG ${problem}`);
  console.error('\nタブの現在地が自分のページを指していない。');
  console.error('下線だけでなく aria-current も誤るため、読み上げで現在地が伝わらない。');
  process.exit(1);
}

console.log('OK  どのページもタブの現在地が自分を指している。');
