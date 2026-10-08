// 勝率バーの**意味が読める形で組まれていること**を検査する。
//
// 守りたいことは3つあり、どれも「クラスが付いているか」では捕まらない。
//
//   1. **縮尺は % で持つ。** px で持つと確率が高いほど塗りが器を越える。
//      実際に起きた（2026-10-02）— バー 62px・半幅 31px に対し、確率は
//      `[0.05, 0.95]` にクランプされるため隔たりは最大45ポイントになり、
//      px 縮尺では 14px ぶんが隣へ溢れていた。合成データの最大が 68% だった
//      ため画面では見えていなかった。
//   2. **軸を背景で描く。** 絶対配置の `::after` は、`border-collapse: collapse`
//      の表で WebKit が包含ブロックを作らず **iOS Safari で1本も出なかった**
//      （2026-10-02）。表はやめたが（スコアボード型。2026-10-08）、
//      **同じ誤りを繰り返さないために背景のままにする。**
//   3. **溝が目盛りの全体を示していること。** スコアボード型ではバーがボードの
//      中にあり、「どこまでが 0〜100 か」を示す器が要る。旧版（データ密度型）は
//      溝を描いていなかったが、あれは表の列に軸を通して器の代わりにしていた。
//
// **`.axis-column` の検査は廃止した**（2026-10-08）。あれは `<table>` の列を
// セルの全高で貫くための仕組みで、スコアボード型には表そのものが無い。
//
// 検査は**部品の原文**を読む。取得をクライアントで行うため、プリレンダされた
// HTML には読み込み中の文しか入らない（詳細設計 5.6）。
//
// 依存を増やさない（Node の標準モジュールだけ）。
import { readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { resolve } from 'node:path';

const APP = resolve(import.meta.dirname, '..', 'app');
const COMPONENTS = resolve(import.meta.dirname, '..', 'components', 'prediction');
const CSS_SOURCE = resolve(APP, 'globals.css');

/** 勝率は `[0.05, 0.95]` にクランプされるため、50% からの隔たりは最大45ポイント */
const MAX_DEVIATION = 45;

const problems = [];

// ── 1. 縮尺は % で持つ ────────────────────────────────────────────
// バーを使う部品を名前で決め打ちしない（一覧の部品名は移行で変わった）。
// `devScale=` を持つファイルすべてを見る。
const BAR_FILES = ['GameBoardList.tsx', 'ProbabilityBar.tsx', 'GameDetailView.tsx'];
let scalesSeen = 0;
for (const file of BAR_FILES) {
  const path = resolve(COMPONENTS, file);
  if (!existsSync(path)) continue;
  const source = await readFile(path, 'utf8');
  for (const scale of source.match(/devScale="([^"]+)"/g) ?? []) {
    scalesSeen += 1;
    const unit = scale.replace(/devScale="|"/g, '').trim();
    if (!unit.endsWith('%')) {
      problems.push(
        `${file}: 縮尺が % でない（${unit}）。`
        + 'px で持つと確率が高いほど塗りが器を越える',
      );
      continue;
    }
    const perPoint = Number.parseFloat(unit);
    if (!Number.isFinite(perPoint) || perPoint <= 0) {
      problems.push(`${file}: 縮尺が読めない（${unit}）`);
      continue;
    }
    // バーの半分（50%）に、最大の隔たりが収まること
    const widest = MAX_DEVIATION * perPoint;
    if (widest > 50) {
      problems.push(
        `${file}: 縮尺 ${unit} では 95% の塗りが ${widest.toFixed(1)}% になり、`
        + 'バーの半分（50%）を越える',
      );
    }
  }
}
if (scalesSeen === 0) {
  problems.push('devScale を持つ部品が1つも見つからない（バーの組み方が変わったか）');
}

// ── 2. 軸を背景で描く ────────────────────────────────────────────
const css = await readFile(CSS_SOURCE, 'utf8');

if (css.includes('.axis-column')) {
  problems.push(
    'globals.css: .axis-column が残っている。'
    + 'スコアボード型では表をやめたため、この仕組みは使わない（基本設計 6.3）',
  );
}

const barRule = css.slice(css.indexOf('.axis-bar'));
const barBlock = barRule.slice(0, barRule.indexOf('}') + 1);

// ── 3. 溝が目盛りの全体を示す ───────────────────────────────────
if (!/background-color:\s*var\(--groove\)/.test(barBlock)) {
  problems.push(
    'globals.css: .axis-bar に溝（--groove）がない。'
    + 'ボードの中のバーは「どこまでが 0〜100 か」を示す器を要する',
  );
}

// 互角帯（±5ポイント）が常に同じ位置に描かれていること
if (!/var\(--band\)/.test(barBlock) || !css.includes('--band-half')) {
  problems.push(
    'globals.css: .axis-bar に互角帯（--band / --band-half）がない。'
    + 'しきい値が画面に出ていないと、互角かどうかを読者が検算できない（要件 8.3）',
  );
}

// 軸は `::after` で描くが、**位置は背景ではなく絶対配置**にした。
// ボードの中ではバーが器であり、`border-collapse` の包含ブロックの問題は起きない。
// それでも**幅1pxで上下いっぱいに通っている**ことは確かめる。
const axisRule = css.slice(css.indexOf('.axis-bar::after'));
const axisBlock = axisRule.slice(0, axisRule.indexOf('}') + 1);
for (const [pattern, message] of [
  [/left:\s*50%/, '軸が 50% の位置にない'],
  [/width:\s*1px/, '軸の幅が 1px でない'],
  [/background:\s*var\(--axis\)/, '軸が --axis で塗られていない'],
  [/top:\s*0/, '軸が上端から始まっていない'],
  [/bottom:\s*0/, '軸が下端まで届いていない'],
]) {
  if (!pattern.test(axisBlock)) problems.push(`globals.css: ${message}`);
}

// ── 4. 塗りは両側を持つ ─────────────────────────────────────────
for (const name of ['.axis-fill-home', '.axis-fill-away']) {
  if (!css.includes(name)) {
    problems.push(`globals.css: ${name} がない（勝率は必ず両チーム分を示す。要件 8.3）`);
  }
}

if (problems.length > 0) {
  for (const problem of problems) console.error(`NG  ${problem}`);
  console.error(
    `\n勝率バーの組み方が ${problems.length} 件、設計と違う（基本設計 6.3 / 詳細設計 5.3）。`,
  );
  process.exit(1);
}

console.log(
  'OK  縮尺は % で、95% の塗りも器に収まる。軸は 50% に幅1pxで上下いっぱいに通り、'
  + '溝と互角帯が目盛りを示している。',
);
