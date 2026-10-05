// `lib/map.ts` の単体テスト（工程11b）。
//
// **依存を増やさない。** Node 24 は TypeScript をそのまま実行でき、`node:test` が
// 組み込みである（vitest を入れると devDependencies が増える）。
//
// **ここを検査する理由。** 時刻変換・遅延判定・丸めは**間違っても画面が壊れず、
// 値だけが静かにずれる**種類の処理である。E2E は工程15 であり、それまでの唯一の
// 歯止めになる。

import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  STALE_HOURS,
  calibrationNote,
  correctCount,
  dateLabel,
  generatedAtLabel,
  isStale,
  shiftDate,
  toAccuracy,
  toClub,
  toGame,
  toGames,
  toReason,
  tipoffLabel,
} from '../map.ts';
import type { GameShape, Meta } from '../source.ts';

// --- 時刻（CLAUDE.md 時刻の扱い） ---

test('開始時刻は JST に直して出す', () => {
  // 19:05 JST = 10:05 UTC
  assert.equal(tipoffLabel('2026-10-07T10:05:00Z'), '19:05');
  // 日付をまたぐ: 00:30 JST = 前日 15:30 UTC
  assert.equal(tipoffLabel('2026-10-06T15:30:00Z'), '00:30');
});

test('時刻が未公表なら null を返す', () => {
  assert.equal(tipoffLabel(null), null);
});

test('読めない時刻を 0 時として出さない', () => {
  // **黙って `00:00` にしない。** 「時刻未定」と「0時開始」は別である
  assert.equal(tipoffLabel('これは時刻ではない'), null);
});

test('日付の見出しは JST の暦日の曜日で出す', () => {
  // 2026-10-07 は水曜日
  assert.equal(dateLabel('2026-10-07'), '10月7日（水）');
});

test('日付の加算が月をまたぐ', () => {
  assert.equal(shiftDate('2026-10-31', 1), '2026-11-01');
  assert.equal(shiftDate('2026-01-01', -1), '2025-12-31');
});

test('生成時刻は JST で出す', () => {
  // 21:02 UTC = 翌日 06:02 JST（要件 8.4 の文言）
  assert.equal(generatedAtLabel('2026-10-04T21:02:00Z'), '10月5日 6:02');
});

// --- 更新遅延（基本設計 4.5） ---

function meta(over: Partial<Meta> = {}): Meta {
  return {
    generatedAt: '2026-10-05T06:00:00Z',
    dataAsOf: '2026-10-04T08:05:00Z',
    lastSuccessAt: '2026-10-05T06:00:00Z',
    lastRunStatus: 'SUCCESS',
    modelVersions: ['winner-v1.1.0'],
    ...over,
  };
}

test('24時間を越えたら遅延と判定する', () => {
  const last = Date.parse('2026-10-05T06:00:00Z');
  assert.equal(isStale(meta(), last + STALE_HOURS * 3600_000 - 1000), false);
  assert.equal(isStale(meta(), last + STALE_HOURS * 3600_000 + 1000), true);
});

test('判定は lastSuccessAt を見る（generatedAt ではない）', () => {
  // **毎日失敗しても `generatedAt` は毎日新しくなる**（基本設計 4.5 で旧版が
  // 踏んだ誤り）。成功していない限り遅延と判定されなければならない
  const stale = meta({
    generatedAt: '2026-10-05T06:00:00Z',
    lastSuccessAt: '2026-10-01T06:00:00Z',
  });
  assert.equal(isStale(stale, Date.parse('2026-10-05T06:00:00Z')), true);
});

test('一度も成功していなければ遅延と判定しない', () => {
  // **判定材料がない。** 推測で「古い」と出さない（詳細設計 3.7）
  assert.equal(isStale(meta({ lastSuccessAt: null })), false);
});

// --- クラブ（詳細設計 4.2 のステップ5） ---

test('表示名が無いときは slug を出す', () => {
  // 当季の `club_seasons` は最初の試合が終わるまで空である。
  // **`clubs.name`（現在の表示名）で埋めない** — 過去試合の表示が遡って変わる
  const club = toClub({ clubId: '692', slug: 'sendai-89ers', name: null, shortName: null });
  assert.deepEqual(club, {
    slug: 'sendai-89ers',
    name: 'sendai-89ers',
    shortName: 'sendai-89ers',
  });
});

// --- 試合 ---

function shape(over: Partial<GameShape> = {}): GameShape {
  return {
    gameId: '506431',
    tipoffAt: '2026-10-07T10:05:00Z',
    status: 'SCHEDULED',
    competition: 'REGULAR',
    home: { clubId: '692', slug: 'a', name: 'ホーム', shortName: 'ホ' },
    away: { clubId: '693', slug: 'b', name: 'アウェイ', shortName: 'ア' },
    prediction: {
      homeWinProb: 0.7394,
      predHomeScore: 83.38,
      predAwayScore: 75.08,
      isProvisional: true,
      isFinal: false,
      isEarlySeason: null,
      modelVersion: 'winner-v1.1.0',
    },
    ...over,
  };
}

test('予想スコアは整数に丸める', () => {
  // MAE 8〜10点に対し小数第1位は精度の誤認を招く（要件 8.3）
  const game = toGame(shape());
  assert.equal(game?.predHomeScore, 83);
  assert.equal(game?.predAwayScore, 75);
});

test('勝率は丸めない', () => {
  // 表示の丸めは `percentPair` が行う。ここで丸めるとバーと数値がずれる
  assert.equal(toGame(shape())?.homeWinProb, 0.7394);
});

test('予測がない試合は null になる', () => {
  // **試合ごと省略しない**（契約。詳細設計 3.3）。空状態は呼び出し側が出す
  assert.equal(toGame(shape({ prediction: null })), null);
});

test('序盤かどうかが不明なら序盤と出さない', () => {
  // **推測で true にしない。** 消化試合数の集計がないため null で来る
  assert.equal(toGame(shape())?.isEarlySeason, false);
});

test('知らない状態は SCHEDULED として扱う', () => {
  assert.equal(toGame(shape({ status: 'LIVE' }))?.status, 'SCHEDULED');
});

test('予測がない試合は一覧から外れる（件数の差が注記になる）', () => {
  const games = toGames({
    gameDate: '2026-10-07',
    games: [shape(), shape({ gameId: 'x', prediction: null })],
    accuracy: null,
  });
  assert.equal(games.length, 1);
});

// --- 的中率（要件 8.3） ---

test('母数が 0 なら的中率を出さない', () => {
  // **母数の併記が要件の条件である。** 0件の的中率は意味を持たない
  assert.equal(toAccuracy({ accuracy: 0.68, brier: 0.2, n: 0 }, 0.527), null);
});

test('ベースラインが無ければ的中率を出さない', () => {
  // **自分の成績だけを見せない**（同じ大きさで隣に置くのが要件 8.3 である）
  assert.equal(toAccuracy({ accuracy: 0.68, brier: 0.2, n: 312 }, null), null);
});

// --- 根拠（詳細設計 2.7） ---

test('強さは 1〜4 に収める', () => {
  assert.equal(toReason({ group: 'a', label: 'b', value: 'c', favors: 'HOME', strength: 0 }).strength, 1);
  assert.equal(toReason({ group: 'a', label: 'b', value: 'c', favors: 'HOME', strength: 9 }).strength, 4);
});

test('知らない favors はホーム側として扱う', () => {
  assert.equal(
    toReason({ group: 'a', label: 'b', value: 'c', favors: '', strength: 2 }).favors,
    'HOME',
  );
});


// --- 的中率ページ（工程14。基本設計 5.2） ---

test('的中した試合数は率と母数から戻す', () => {
  // API は件数を返さない（`accuracy_summary` は率と母数だけ）
  assert.equal(correctCount(223 / 312, 312), 223);
  assert.equal(correctCount(0.5, 2), 1);
  assert.equal(correctCount(0, 312), 0);
  assert.equal(correctCount(1, 312), 312);
});

test('較正の言い換えは開きをポイントで出す', () => {
  // **`%` は勝率専用である**（要件 8.3）。2つの率の差は「ポイント」
  assert.equal(calibrationNote(0.65, 0.636), '予想を 1.4ポイント下回っています。');
  assert.equal(calibrationNote(0.75, 0.77), '予想を 2.0ポイント上回っています。');
});

test('較正の言い換えに大きさの形容を入れない', () => {
  // 「やや」は閾値を要し、それは設計文書にない定数になる
  const cases: [number, number][] = [
    [0.7, 0.4],
    [0.7, 0.69],
    [0.5, 0.9],
  ];
  for (const [predicted, actual] of cases) {
    const note = calibrationNote(predicted, actual);
    assert.ok(!note.includes('やや'), note);
    assert.ok(!note.includes('大きく'), note);
  }
});

test('表示する桁で 0.0 になる開きは「予想どおり」とする', () => {
  // `0.0ポイント下回っています` は、開きが無いと言いながら向きを主張する
  assert.equal(calibrationNote(0.65, 0.65), '予想どおりです。');
  assert.equal(calibrationNote(0.65, 0.6502), '予想どおりです。');
  // 丸めて 0.1 になる開きは向きを出す
  assert.equal(calibrationNote(0.65, 0.6506), '予想を 0.1ポイント上回っています。');
});

test('ローカル D1 の実応答で的中数と言い換えが出る', () => {
  // **本番の形で確かめた値を固定する**（2026-10-05。ローカル D1 の照合2件。
  // `/api/v1/accuracy` の実応答から取った）。画面の描画は E2E（工程15）まで
  // 検査できないため、**値を作る側をここで止める**
  assert.equal(correctCount(0.5, 2), 1);
  assert.equal(calibrationNote(0.24, 1), '予想を 76.0ポイント上回っています。');
  assert.equal(calibrationNote(0.82, 1), '予想を 18.0ポイント上回っています。');
});
