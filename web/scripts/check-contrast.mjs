// 要件 8.1 / 受け入れ基準 A-06 の一部。両テーマでコントラスト比を CI で検証する。
// 目視のスナップショット確認では必ず見落とすため、トークンの単体検査として独立させる（基本設計 6.1）。
// 依存を増やさないため Node 標準のみで書く。
//
// 2026-09-29 に方向性A「データ密度型」を採用した。検査の構造が旧版から2つ変わっている。
//
// 1. **`--axis` は「地に対して」ではなく「`--band` に対して」測る。**
//    50%軸は必ず互角帯の上を通るため。制作時に `#8E8676` を置いたところ、紙には
//    3.31:1 あったが帯の上では 2.61:1 しかなかった。**目視では気づけない。**
// 2. **テキストと線を別の系列として扱う。** 旧版は `--text-4`（非テキスト専用）を
//    テキスト色の系列に混ぜていたため、誤用の走査が必要だった。新トークンでは
//    `--rule` / `--rule-soft` / `--axis` が線の系列で、走査の対象もこちらに移る。
import { readFileSync, readdirSync } from 'node:fs';

const CSS = readFileSync(new URL('../app/globals.css', import.meta.url), 'utf8');

/** `:root` と `:root[data-theme='dark']` のブロックから `--token: value` を読む。 */
function tokens(selector) {
  const start = CSS.indexOf(selector);
  if (start < 0) throw new Error(`セレクタが見つからない: ${selector}`);
  const open = CSS.indexOf('{', start);
  const close = CSS.indexOf('}', open);
  const out = {};
  for (const line of CSS.slice(open + 1, close).split('\n')) {
    const m = /^\s*(--[a-z0-9-]+):\s*([^;]+);/.exec(line);
    if (m) out[m[1]] = m[2].trim();
  }
  return out;
}

function parseColor(value) {
  const hex = /^#([0-9a-f]{6})$/i.exec(value);
  if (hex) {
    const n = parseInt(hex[1], 16);
    return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255, a: 1 };
  }
  const rgba = /^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)$/.exec(value);
  if (rgba) {
    return {
      r: +rgba[1], g: +rgba[2], b: +rgba[3],
      a: rgba[4] === undefined ? 1 : +rgba[4],
    };
  }
  throw new Error(`未対応の色表記: ${value}`);
}

/** 半透明の色は背景の上に合成する。合成しないと比が実際より良く出る。 */
function over(top, bottom) {
  return {
    r: top.r * top.a + bottom.r * (1 - top.a),
    g: top.g * top.a + bottom.g * (1 - top.a),
    b: top.b * top.a + bottom.b * (1 - top.a),
    a: 1,
  };
}

function luminance({ r, g, b }) {
  const f = (c) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
}

function ratio(fg, bg) {
  const [a, b] = [luminance(fg), luminance(bg)].sort((x, y) => y - x);
  return (a + 0.05) / (b + 0.05);
}

// ── テキスト: 両テーマで 4.5:1 以上 ──────────────────────────────
// 画面で実際に使う前景と背景の対だけを並べる。`--tint` を含めるのは、互角の行と
// 合計行が `--tint` の地で、その上に `--ink-3` のラベルが乗るため（最小値がここに出る）。
const TEXT_PAIRS = [
  ['--ink', '--paper'], ['--ink', '--panel'], ['--ink', '--tint'],
  ['--ink-2', '--paper'], ['--ink-2', '--panel'], ['--ink-2', '--tint'],
  ['--ink-3', '--paper'], ['--ink-3', '--panel'], ['--ink-3', '--tint'],
  ['--home', '--paper'], ['--home', '--panel'],
  ['--away', '--paper'], ['--away', '--panel'],
  ['--warn', '--paper'], ['--warn', '--panel'], ['--warn', '--warn-bg'],
  // H / A の記号は `--panel` の文字を `--home` / `--away` の地に置く（GameTable）
  ['--panel', '--home'], ['--panel', '--away'],
];

// ── 意味を持つ非テキスト: 3:1 以上 ──────────────────────────────
// **`--axis` は `--band` に対して測る。** 50%軸は必ず帯の上を通る。
// バーの塗り（`--home` / `--away`）も帯の上に乗るため、帯に対して検査する。
const GRAPHIC_PAIRS = [
  ['--axis', '--band'],
  ['--home', '--band'],
  ['--away', '--band'],
];

// ── 装飾的な仕切り: 基準を課さない ─────────────────────────────
// `--rule` / `--rule-soft` は情報を持たない。同じ情報を余白と見出しが伝える。
// 値は参考として出し、**文字色に使われていないか**は下の走査で止める。
const DECORATIVE = ['--rule', '--rule-soft', '--band'];

const MIN_TEXT = 4.5;
const MIN_GRAPHIC = 3;

let failed = 0;
const textRatios = [];

for (const [theme, selector] of [['light', ':root {'], ['dark', ":root[data-theme='dark']"]]) {
  const t = tokens(selector);
  const panel = parseColor(t['--panel']);
  const measure = (fgName, bgName) => {
    // 半透明の背景トークンは panel の上に置かれる前提で合成する
    const bg = over(parseColor(t[bgName]), panel);
    const fg = over(parseColor(t[fgName]), bg);
    return ratio(fg, bg);
  };

  for (const [fgName, bgName] of TEXT_PAIRS) {
    const value = measure(fgName, bgName);
    const ok = value >= MIN_TEXT;
    if (!ok) failed += 1;
    textRatios.push({ theme, value });
    console.log(
      `${ok ? 'OK  ' : 'NG  '}${theme.padEnd(5)} 文字 ${fgName.padEnd(10)} on ${bgName.padEnd(11)}` +
      ` ${value.toFixed(2)}:1 （4.5 以上）`,
    );
  }

  for (const [fgName, bgName] of GRAPHIC_PAIRS) {
    const value = measure(fgName, bgName);
    const ok = value >= MIN_GRAPHIC;
    if (!ok) failed += 1;
    console.log(
      `${ok ? 'OK  ' : 'NG  '}${theme.padEnd(5)} 図形 ${fgName.padEnd(10)} on ${bgName.padEnd(11)}` +
      ` ${value.toFixed(2)}:1 （3 以上）`,
    );
  }

  for (const name of DECORATIVE) {
    console.log(
      `参考 ${theme.padEnd(5)} 装飾 ${name.padEnd(10)} on --paper     ` +
      ` ${measure(name, '--paper').toFixed(2)}:1 （情報を持たない。基準を課さない）`,
    );
  }
}

// ── 用途の逸脱を止める ────────────────────────────────────────
// 線のトークンを文字色に使った時点で、最も読みにくい色に本文が乗る
// （基本設計 6.1 が名指しで禁じている混同）。
const sources = [];
const walk = (dir) => {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = `${dir}/${entry.name}`;
    if (entry.isDirectory()) walk(path);
    else if (/\.(tsx?|css)$/.test(entry.name)) sources.push(path);
  }
};
for (const dir of ['app', 'components', 'lib']) {
  try {
    walk(new URL(`../${dir}`, import.meta.url).pathname);
  } catch {
    // まだ存在しないディレクトリは飛ばす
  }
}
const BANNED_TEXT_CLASSES = /\btext-(rule|rule-soft|band|axis)\b/;
for (const path of sources) {
  const body = readFileSync(path, 'utf8');
  // `globals.css` は `.axis-bar::after` で `--axis` を線として使う。文字ではない
  if (path.endsWith('globals.css')) continue;
  if (BANNED_TEXT_CLASSES.test(body)) {
    console.error(`NG  線のトークンを文字色に使っている: ${path.replace(/.*\/web\//, '')}`);
    failed += 1;
  }
}

// 旧トークンが残っていないことも見る。廃止したものが復活すると、
// Tailwind の橋渡しに無い名前なので**無色で描かれて気づけない**。
const REMOVED = /\b(text|bg|border)-(text|text-2|text-3|text-4|accent|accent-bg|track|surface)\b/;
for (const path of sources) {
  if (REMOVED.test(readFileSync(path, 'utf8'))) {
    console.error(`NG  廃止したトークンを使っている: ${path.replace(/.*\/web\//, '')}`);
    failed += 1;
  }
}

if (failed > 0) {
  console.error(`\n基準を満たさない項目が ${failed} 件ある（基本設計 6.1）。`);
  process.exit(1);
}

const min = textRatios.reduce((a, b) => (a.value < b.value ? a : b));
console.log(
  `\n両テーマでテキストが 4.5:1 以上（最小 ${min.value.toFixed(2)}:1 / ${min.theme}）、` +
  '意味を持つ図形が 3:1 以上あり、線のトークンの誤用もない。',
);
