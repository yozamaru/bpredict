// **個人スタッツの注記が省略されていないこと**を検査する。
//
// 3つある。どれも「隠さない」ために置いたもので、**消えても画面は壊れない** —
// 検査がなければ、次に部品を書き換えたときに静かに落ちる。
//
//   1. 固定注記（要件 4.5.4）— 個人予測が能力や評価を示すものではないこと。
//      **これは免責の一部である**（省略すると A-07 の趣旨に反する）
//   2. ST / BS の誤差（要件 6.8.6）— 予測が平均に近い値になること
//   3. **成功率の誤差（2026-10-05 に足した）** — 1試合の試投数が少なく、
//      `fg3_pct` と `ft_pct` は学習しないベースラインに負ける（詳細設計 2.3.1）。
//      **ST / BS と同じ扱いにする**という運営者の判断である
//
// **検査は部品の原文を読む。** 個人スタッツはクライアントで静的JSON / API を
// 読むため、プリレンダされた HTML には読み込み中の文しか入らない（`check-bar.mjs`
// と同じ理由）。
import { readFileSync } from 'node:fs';

const FILE = 'components/prediction/PlayerStatTable.tsx';

/** 探す文言。**完全一致ではなく含むかを見る**（前後の空白と改行を許す） */
const REQUIRED = [
  {
    what: '固定注記（要件 4.5.4）',
    text: '選手の能力や評価を示すものではありません',
  },
  {
    what: 'ST / BS の誤差（要件 6.8.6）',
    text: 'スティールとブロックは1試合あたりの回数が少なく',
  },
  {
    what: '成功率の誤差（詳細設計 2.3.1）',
    text: '1試合の成功率は試投数が少なく、誤差が大きい項目です',
  },
];

const source = readFileSync(FILE, 'utf8');
const missing = REQUIRED.filter(({ text }) => !source.includes(text));

if (missing.length > 0) {
  console.error(`${FILE} から注記が落ちている:`);
  for (const { what, text } of missing) {
    console.error(`  - ${what}: 「${text}」`);
  }
  console.error('\n省略しない（要件 4.5.4 / 6.8.6 / 詳細設計 2.3.1）。');
  process.exit(1);
}

console.log(`注記 ${REQUIRED.length}件すべてが ${FILE} にある`);
