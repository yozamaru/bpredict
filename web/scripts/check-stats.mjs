// 戦績・スタッツの表示規約を検査する（要件 A-19 / 3.1.1 / 8.3。詳細設計 5.3 / 6.4.2）。
//
// **検査は部品の原文を読む。** これらはクライアントで API を読むため、プリレンダ
// された HTML には読み込み中の文しか入らない（`check-notes.mjs` と同じ理由）。
//
// **原文を読む検査は出力を読むより弱い**（実際に描かれたかは見ていない）。それでも
// 下の4つは「原文に何が書いてあるか」で決まる誤りであり、変異で確かめられる。
import { existsSync, readFileSync, readdirSync } from 'node:fs';

const SUMMARY = 'components/stats/StatSummaryTable.tsx';
const ROSTER = 'components/stats/RosterTable.tsx';
const NOTES = 'components/stats/StatNotes.tsx';
const PLAYER = 'components/stats/PlayerView.tsx';
const TEAM = 'components/prediction/TeamView.tsx';

const read = (file) => ({ file, source: readFileSync(file, 'utf8') });

const failures = [];
const check = (what, ok) => {
  if (!ok) failures.push(what);
};

const summary = read(SUMMARY);
const roster = read(ROSTER);
const notes = read(NOTES);
const player = read(PLAYER);
const team = read(TEAM);

// 1. **率は分数と併記する**（要件 8.3）。単独の率を出さない。
//    `Shooting` が `m / a` と `pct` の両方を描いていること
check(
  `${SUMMARY}: 率を分数と併記していない`,
  /num\(value\.m\)[\s\S]{0,40}num\(value\.a\)/.test(summary.source)
    && summary.source.includes('value.pct !== null'),
);

// 2. **試投数が0のときだけ率を出さない。** 閾値（`>= 4` など）を入れていないこと。
//    実績に閾値を設けない — 要件 8.3 の閾値は**予測値**についてのものである。
//
//    **数値との比較をファイル全体で禁じる。** `.a >= 4` だけを探す版では
//    `(value.a ?? 0) >= 4` を取りこぼした（変異試験で素通りした）。この2つの部品は
//    値を整形するだけで、数値と比べる理由がない
for (const { file, source } of [summary, roster]) {
  check(
    `${file}: 数値と比較している（実績の率に閾値を設けない）`,
    !/(>=|>)\s*\d/.test(source),
  );
}

// 3. **母数（試合数）を同じ行に出す**（要件 8.3）。1試合平均は母数なしで読めない
check(
  `${SUMMARY}: 試合数の列がない`,
  summary.source.includes('{row.games}') && summary.source.includes('試合'),
);
check(
  `${ROSTER}: 試合数の列がない`,
  roster.source.includes('{entry.games}'),
);

// 4. **順位の数字を振らない**（要件 3.1.1。ランキングにしない）
for (const { file, source } of [summary, roster]) {
  check(
    `${file}: 順位の数字を振っている`,
    !/\bindex\s*\+\s*1\b/.test(source) && !/at\s*\+\s*1\b/.test(source),
  );
}

// 5. 注記3件（要件 3.1.1 / 5.3 / 6.9）。**消えても画面は壊れない**
const REQUIRED_NOTES = [
  { what: '通算の範囲（要件 3.1.1）', text: '通算は 2016-17 シーズン以降' },
  { what: '不戦敗を含まないこと（要件 5.3）', text: '不戦敗として記録された試合は含みません' },
  { what: '実績であり評価でないこと（要件 6.9）', text: '選手の能力や評価を示すものではありません' },
];
for (const { what, text } of REQUIRED_NOTES) {
  check(`${NOTES}: 注記が落ちている — ${what}`, notes.source.includes(text));
}

// 使われていなければ意味がない
check(`${PLAYER}: 注記を描いていない`, player.source.includes('StatNotes'));
check(`${TEAM}: 注記を描いていない`, team.source.includes('StatNotes'));

// 6. **予測値を混ぜない**（選手ページに出るのはすべて実績である）
for (const key of ['availProb', 'homeWinProb', 'predMinutes', 'predHomeScore']) {
  check(`${PLAYER}: 予測値（${key}）を出している`, !player.source.includes(key));
}

// 7. **CSV にある選手の数だけページが生成されていること**（要件 8.2 / 詳細設計 4.14）。
//    `staticPlayerIds()` が空を返しても**画面は壊れず、ページが静かに0件になる**
//    （リンクは `canLink` で消え、`test:links` も落ちない）
const CSV = 'data/players.csv';
if (existsSync(CSV) && existsSync('out/players')) {
  const listed = readFileSync(CSV, 'utf8').trim().split('\n').length - 1; // ヘッダを引く
  const built = readdirSync('out/players', { withFileTypes: true })
    .filter((e) => e.isDirectory()).length;
  check(
    `out/players の件数が players.csv と合わない（CSV ${listed} / 生成 ${built}）`,
    listed === built,
  );
}

if (failures.length > 0) {
  console.error('戦績・スタッツの表示規約に違反している:');
  for (const line of failures) console.error(`  - ${line}`);
  console.error(
    '\n出すのは集計値だけで、母数と分数を必ず併記する（要件 A-19 / 3.1.1 / 8.3）。',
  );
  process.exit(1);
}

console.log('戦績・スタッツ: 率の併記・母数・注記3件・予測値の不在を確認した');
