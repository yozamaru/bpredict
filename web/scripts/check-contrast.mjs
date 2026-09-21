// 要件 8.1 / 受け入れ基準 A-06 の一部。両テーマで本文のコントラスト比 4.5:1 以上を CI で検証する。
// 目視のスナップショット確認では必ず見落とすため、トークンの単体検査として独立させる（基本設計 6.1）。
// 依存を増やさないため Node 標準のみで書く。
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

// 検査する組み合わせ。画面で実際に使う前景と背景の対だけを並べる。
const PAIRS = [
  ['--text', '--bg'], ['--text', '--surface'],
  ['--text-2', '--bg'], ['--text-2', '--surface'],
  ['--text-3', '--bg'], ['--text-3', '--surface'],
  ['--accent', '--bg'], ['--accent', '--surface'],
  ['--accent', '--accent-bg'],
  ['--warn', '--warn-bg'],
];

// `--text-4` は**罫線など非テキスト専用**（基本設計 6.1）。文字に使わない前提なので
// 4.5:1 を課さない。値は参考として出し、用途の逸脱は下の走査で止める。
const NON_TEXT = ['--text-4'];
const MIN_RATIO = 4.5;

let failed = 0;
for (const [theme, selector] of [['light', ':root {'], ['dark', ":root[data-theme='dark']"]]) {
  const t = tokens(selector);
  const surface = parseColor(t['--surface']);
  const measure = (fgName, bgName) => {
    // 半透明の背景トークンは surface の上に置かれる前提で合成する
    const bg = over(parseColor(t[bgName]), surface);
    const fg = over(parseColor(t[fgName]), bg);
    return ratio(fg, bg);
  };
  for (const [fgName, bgName] of PAIRS) {
    const value = measure(fgName, bgName);
    const ok = value >= MIN_RATIO;
    if (!ok) failed += 1;
    console.log(
      `${ok ? 'OK  ' : 'NG  '}${theme.padEnd(5)} ${fgName.padEnd(9)} on ${bgName.padEnd(12)}` +
      ` ${value.toFixed(2)}:1`,
    );
  }
  for (const fgName of NON_TEXT) {
    for (const bgName of ['--bg', '--surface']) {
      console.log(
        `参考 ${theme.padEnd(5)} ${fgName.padEnd(9)} on ${bgName.padEnd(12)}` +
        ` ${measure(fgName, bgName).toFixed(2)}:1 （非テキスト専用。基準を課さない）`,
      );
    }
  }
}

// 用途の逸脱を止める。`--text-4` を文字色として使った時点で、
// 最も読みにくい色に本文が乗る（基本設計 6.1 が名指しで禁じている混同）。
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
const misuse = sources.filter((path) => /\btext-text-4\b/.test(readFileSync(path, 'utf8')));
for (const path of misuse) {
  console.error(`NG  --text-4 を文字色に使っている: ${path.replace(/.*\/web\//, '')}`);
  failed += 1;
}

if (failed > 0) {
  console.error(`\n基準を満たさない項目が ${failed} 件ある（基本設計 6.1）。`);
  process.exit(1);
}
console.log('\n両テーマで本文のコントラストが 4.5:1 以上あり、--text-4 の誤用もない。');
