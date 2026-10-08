// 試合詳細ページが**一覧のリンクと食い違っていないこと**を検査する。
//
// 守りたいのは1つだけ — **一覧に出ている試合には必ずページがある。**
//
// 2026-10-08 に運営者が踏んだ 404 がこれである。一覧（`GameBoardList`）は
// 全試合に無条件でリンクを張る一方、ページの出典は窓（当日＋7日）だった。
// **窓から出た試合の詳細ファイルは削除される**ため（詳細設計 3.7）、昨日の
// 試合を押すと 404 になった。出典を**溜まる索引**に変えたが（5.6）、
// 索引と一覧がずれたら同じことが起きる。
//
// `test:links` では捕まらない。あちらが読むのは**生成された HTML** で、
// 一覧は取得をクライアントで行うためリンクが出力に現れない（詳細設計 5.6）。
// したがって**出典のデータ同士**を突き合わせる。
//
// 依存を増やさない（Node の標準モジュールだけ）。
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { join, resolve } from 'node:path';

const WEB = resolve(import.meta.dirname, '..');
const DATA = join(WEB, 'public', 'data');
const INDEX = join(DATA, 'games', 'index.json');

const failures = [];
const check = (message, ok) => {
  if (!ok) failures.push(message);
};

/** 索引を読む（`lib/routes.ts` と同じ形。あちらを変えたらここも落ちる） */
function indexIds() {
  if (!existsSync(INDEX)) return null;
  const body = JSON.parse(readFileSync(INDEX, 'utf8'));
  return new Set(Array.isArray(body?.gameIds) ? body.gameIds : []);
}

/** 一覧 1枚から「予測を出した試合」のIDを拾う */
function predictedIn(path) {
  const games = JSON.parse(readFileSync(path, 'utf8'))?.data?.games;
  if (!Array.isArray(games)) return [];
  return games.filter((g) => g?.prediction != null).map((g) => String(g.gameId));
}

// ── 0. 一覧がある以上、索引もある ───────────────────────────────
// **一覧が無いリポジトリ（初回ビルド）だけを免除する。** 索引が無いだけを
// 免除すると、索引を消した変更が素通りして**一覧のリンク全部が 404 になる**
const lists = [];
if (existsSync(join(DATA, 'today.json'))) lists.push(join(DATA, 'today.json'));
const scheduleDir = join(DATA, 'schedule');
if (existsSync(scheduleDir)) {
  for (const name of readdirSync(scheduleDir).sort()) {
    if (name.endsWith('.json')) lists.push(join(scheduleDir, name));
  }
}

const ids = indexIds();

if (lists.length === 0 && ids === null) {
  console.log('試合詳細: 静的JSON が書き出されていないため検査を省いた');
  process.exit(0);
}

if (ids === null) {
  console.error('試合詳細ページが一覧と食い違っている:');
  console.error('  - data/games/index.json が無い（一覧はあるのにページが1枚も作られない）');
  process.exit(1);
}

// ── 1. 一覧に出ている試合はすべて索引にある ─────────────────────
// これが**無条件のリンクを許している唯一の根拠**である。索引に入る条件
// （`prediction != null`）と、一覧が行を出す条件（`toGame` が null を落とす）が
// 同じ定義だから成り立つ。片方を変えたらここで落ちる。
for (const path of lists) {
  for (const id of predictedIn(path)) {
    check(
      `${path.slice(DATA.length + 1)} の試合 ${id} が索引に無い`
      + '（一覧はリンクを張るのに、ページが作られない）',
      ids.has(id),
    );
  }
}

// ── 2. 索引の件数だけページが生成されている ─────────────────────
// `staticGameIds()` が空を返しても**画面は壊れず、ページが静かに0件になる**
// （`check-stats.mjs` の `players.csv` と同じ理由）
const built = join(WEB, 'out', 'games');
if (existsSync(built)) {
  const pages = readdirSync(built, { withFileTypes: true }).filter((e) => e.isDirectory()).length;
  check(
    `out/games の件数が索引と合わない（索引 ${ids.size} / 生成 ${pages}）`,
    ids.size === pages,
  );
}

if (failures.length > 0) {
  console.error('試合詳細ページが一覧と食い違っている:');
  for (const line of failures) console.error(`  - ${line}`);
  console.error(
    '\n一覧に出ている試合には必ずページがある（詳細設計 5.6）。'
    + '出典は data/games/index.json で、バッチが溜めてコミットする。',
  );
  process.exit(1);
}

console.log(
  `試合詳細: 索引 ${ids.size}件。一覧の全試合が索引にあり、生成件数も一致している`,
);
