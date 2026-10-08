// 生成した HTML で、**ヘッダーの操作が名前と状態を持っている**ことを検査する。
//
// 見るのは2つ。**タブの現在地**（下記）と、**テーマ切替のアイコンの名前**
// （末尾）。どちらも**消えても画面は壊れず、読み上げだけが静かに失われる。**
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

// ── テーマ切替のアイコン ──────────────────────────────────────────
//
// **文字をやめたので、名前は `aria-label` しか持っていない**（詳細設計 5.3）。
// これが落ちると**ボタンに名前が無くなり、読み上げでは何も読まれない。**
// 画面は何も変わらないため、目で見ても気づけない。
//
// `aria-pressed` も見る。アイコンは形で状態を示すが、**支援技術には
// `aria-pressed` しか届かない。**
const home = await readFile(pageOf('/'), 'utf8');
const toggle = /<button\b[^>]*aria-pressed[^>]*>([\s\S]*?)<\/button>/.exec(home);

if (toggle === null) {
  problems.push('テーマ切替: aria-pressed を持つ <button> が無い（詳細設計 5.2）');
} else {
  const [tag] = /<button\b[^>]*>/.exec(toggle[0]);
  const label = /aria-label="([^"]*)"/.exec(tag);
  if (label === null || label[1].trim() === '') {
    problems.push(
      'テーマ切替: aria-label が無い。'
      + 'アイコンだけのボタンには名前が残らず、読み上げで何も読まれない（要件 8.6）',
    );
  }
  // 中身はアイコンだけで、**読み上げ対象を二重に持たない**
  if (!/<svg\b[^>]*aria-hidden="true"/.test(toggle[1])) {
    problems.push(
      'テーマ切替: アイコンに aria-hidden="true" が無い。'
      + 'ボタンの名前と二重に読まれる（詳細設計 5.2 の確率ブロックと同じ理由）',
    );
  }
  // 44×44px（要件 8.6）。**アイコンを小さくしてもタップ対象は縮めない**
  if (!/min-h-11/.test(tag) || !/min-w-11/.test(tag)) {
    problems.push('テーマ切替: タップ対象が 44×44px を保っていない（要件 8.6）');
  }
  // **向きを固定する**（詳細設計 5.3）。アイコンは「いまの状態」ではなく
  // **「押したら切り替わる先」**を出す。既定はライトなので、サーバが描く
  // この1枚は**月**でなければならない。
  //
  // **一度逆に実装した**（2026-10-08。運営者の指摘で気づいた）。画面を見れば
  // 分かる誤りだが、**どちらの慣習も成り立つように見える**ため、作り直すたびに
  // 反転しうる。決めた向きをここに置く。
  //
  // 太陽だけが `<circle>` を持つ（月は欠けた円を `<path>` 1本で描く）。
  if (/<circle\b/.test(toggle[1])) {
    problems.push(
      'テーマ切替: ライト（既定）で太陽が出ている。'
      + '**アイコンは切り替わる先を出す** — ライトでは月、ダークでは太陽（詳細設計 5.3）',
    );
  }
}

console.log(`タブの現在地を ${TABS.length} ページ、テーマ切替の名前と状態を検査した`);

if (problems.length > 0) {
  for (const problem of problems) console.error(`NG ${problem}`);
  console.error('\nヘッダーの操作が名前または状態を失っている。');
  console.error('いずれも画面は変わらず、読み上げだけが静かに落ちる（要件 8.6）。');
  process.exit(1);
}

console.log('OK  どのページもタブの現在地が自分を指し、テーマ切替は名前と状態を持っている。');
