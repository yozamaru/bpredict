// 生成した HTML で、**優勢バーが列から溢れないこと**と**軸が列の全高に通っていること**
// を検査する（詳細設計 5.3）。
//
// 2026-10-02 に実機で2つの誤りが出た。
//
//   1. 軸がバーの内側（高さ12px）にしかなく、塗りの隣に互角帯の片側だけが見えるため
//      **「2色の積み上げバー」に読めた**。基準の線が無く、どこが 50% かも分からない
//   2. 縮尺が **1ポイント = 1px** で、確率がクランプの上限（95%）に寄ると
//      隔たり45ポイント = 45px になり、半幅 31px を越えて**隣の列へ溢れる**。
//      合成データの最大が 68% だったため画面では見えていなかった
//
// **2 は目で見ても分からない。** 溢れる確率のデータが画面に無いためである。
// 縮尺が % であれば、バーの半分が 50ポイントに対応するので構造的に収まる。
//
// そして 1 の直し方そのものが3つめの誤りだった。
//
//   3. 軸を `<td>` の `::after`（絶対配置）で描いた。`border-collapse: collapse` の
//      表では **WebKit が `<td>` の `position: relative` に包含ブロックを作らない**
//      ため、Chrome では出て **iOS Safari では1本も出なかった**（運営者が実機で指摘）。
//
// **3 はクラスの有無を見るだけでは捕まらない。** `axis-column` は付いていたのに
// 描かれていなかった。したがって**描き方（CSS）も検査する** — 背景で描いていること、
// 絶対配置に戻っていないこと。同じ列の互角帯は最初から背景グラデーションであり、
// Safari でも出ていた（`.axis-bar`）。
//
// E2E は工程15であり、それまでこの種の誤りを捕まえるものがない。
// **静的出力なら、生成物を読めば機械で判定できる。**
//
// 依存を増やさない（Node の標準モジュールだけ）。
import { readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { join, resolve } from 'node:path';

const OUT = resolve(import.meta.dirname, '..', 'out');
const CSS_SOURCE = resolve(import.meta.dirname, '..', 'app', 'globals.css');

/** 一覧を出すページ。`/schedule/<date>/` は代表1枚で足りる */
const PAGES = ['index.html', 'results/index.html'];

/** 確率は後処理で [0.05, 0.95] にクランプされる（詳細設計 4.6）→ 隔たりは最大45 */
const MAX_DEVIATION = 45;

const problems = [];
let bars = 0;
let cells = 0;

for (const page of PAGES) {
  const path = join(OUT, page);
  if (!existsSync(path)) continue;
  const html = await readFile(path, 'utf8');

  // 1. 優勢のセルに `axis-column` が付いていること（軸がセルの全高に通る）
  for (const cell of html.match(/<td\b[^>]*rowspan="2"[^>]*>/gi) ?? []) {
    // 時刻・状態のセルも rowspan=2 である。バーを含むセルだけを見る
    const after = html.slice(html.indexOf(cell) + cell.length, html.indexOf(cell) + cell.length + 600);
    if (!after.includes('axis-bar')) continue;
    cells += 1;
    if (!cell.includes('axis-column')) {
      problems.push(`${page}: 優勢のセルに axis-column がない（軸が列を貫かない）`);
    }
  }

  // 2. 塗りの縮尺が % であり、最大の隔たりでも半幅（50%）に収まること
  for (const fill of html.match(/<span\b[^>]*axis-fill[^>]*>/g) ?? []) {
    bars += 1;
    const dev = fill.match(/--dev:\s*([0-9.]+)/);
    const scale = fill.match(/--dev-scale:\s*([^;"]+)/);
    if (dev === null || scale === null) {
      problems.push(`${page}: 塗りに --dev / --dev-scale がない`);
      continue;
    }
    const unit = scale[1].trim();
    if (!unit.endsWith('%')) {
      problems.push(
        `${page}: 縮尺が % でない（${unit}）。` +
          `px だと隔たり ${MAX_DEVIATION} ポイントで列から溢れる`,
      );
      continue;
    }
    const perPoint = Number.parseFloat(unit);
    if (!Number.isFinite(perPoint) || perPoint <= 0) {
      problems.push(`${page}: 縮尺が読めない（${unit}）`);
      continue;
    }
    // バーの半分が 50% である。最大の隔たりがそれを越えてはならない
    const widest = MAX_DEVIATION * perPoint;
    if (widest > 50) {
      problems.push(
        `${page}: 隔たり ${MAX_DEVIATION} ポイントで幅 ${widest}% となり半幅 50% を越える`,
      );
    }
    if (Number.parseFloat(dev[1]) > 50) {
      problems.push(`${page}: 隔たりが 50 を越えている（${dev[1]}）`);
    }
  }
}

// 3. 軸の**描き方**を検査する。クラスが付いていても描かれないことがある
const css = await readFile(CSS_SOURCE, 'utf8');
const rule = css.slice(css.indexOf('.axis-column'));
const block = rule.slice(0, rule.indexOf('}') + 1);
if (!block.includes('background-image')) {
  problems.push('globals.css: .axis-column が背景で軸を描いていない');
}
if (!/background-size:\s*1px\s+100%/.test(block)) {
  problems.push('globals.css: .axis-column の軸が全高（1px 100%）でない');
}
// `::after` + `position: relative` に戻すと iOS Safari で1本も出なくなる
if (css.includes('.axis-column::after') || /\.axis-column\s*\{[^}]*position:\s*relative/.test(css)) {
  problems.push(
    'globals.css: .axis-column を絶対配置で描いている' +
      '（border-collapse の表では WebKit が包含ブロックを作らない）',
  );
}

if (bars === 0) {
  console.error('check-bar: 優勢バーが1つも見つからない（out/ を作ったか？）');
  process.exit(1);
}

console.log(`優勢バー ${bars} 本 / 優勢セル ${cells} 件を検査した`);
if (problems.length > 0) {
  for (const problem of problems) console.error(`NG  ${problem}`);
  process.exit(1);
}
console.log('OK  軸が背景で列の全高に通り、どの確率でも塗りが列に収まる。');
