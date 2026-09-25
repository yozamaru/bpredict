// **表示確認用の合成データ。** 実サイトから取得した値ではなく、クラブ名も架空である。
// 工程11a はデザインシステムと画面の骨組みだけを扱い、実データへの結線は 11b。
// 静的JSON のスキーマ（U-09）はここで決めない（詳細設計 9章）。
import type { AccuracyView, Club, GameView } from '@/lib/view';

export const SAMPLE_DATE_LABEL = '9月22日（火）';

/**
 * 当日の日付と、前後の日付。**`/` から日付別へ辿れるようにする**
 * （基本設計 5.1 の `/ ─→ /schedule/[date]`）。
 *
 * 11b では静的JSON が持つ日付から導く。**「いま」から計算しない** —
 * 静的配信では「いま」を知らず、時刻で変わる表示はキャッシュと噛み合わない。
 */
export const SAMPLE_DAY = {
  date: '2026-09-22',
  previous: null as string | null,
  next: '2026-09-23',
  nextLabel: '9月23日（水）',
};

export const SAMPLE_ACCURACY: AccuracyView = { rate: 0.682, n: 312 };

export const SAMPLE_GAMES: GameView[] = [
  {
    gameId: 'demo-1',
    tipoffLabel: '19:05',
    status: 'SCHEDULED',
    home: { slug: 'demo-alphas', name: '架空アルファーズ', shortName: '架空A' },
    away: { slug: 'demo-bulls', name: '架空ブルズ', shortName: '架空B' },
    homeWinProb: 0.68,
    predHomeScore: 84,
    predAwayScore: 78,
    isProvisional: true,
    isFinal: false,
    isEarlySeason: false,
  },
  {
    gameId: 'demo-2',
    tipoffLabel: '17:05',
    status: 'SCHEDULED',
    home: { slug: 'demo-cats', name: '架空キャッツ', shortName: '架空C' },
    away: { slug: 'demo-dolphins', name: '架空ドルフィンズ', shortName: '架空D' },
    homeWinProb: 0.55,
    predHomeScore: 81,
    predAwayScore: 80,
    isProvisional: true,
    isFinal: false,
    isEarlySeason: true,
  },
  {
    gameId: 'demo-3',
    tipoffLabel: '14:05',
    status: 'SCHEDULED',
    home: { slug: 'demo-eagles', name: '架空イーグルス', shortName: '架空E' },
    away: { slug: 'demo-foxes', name: '架空フォックス', shortName: '架空F' },
    homeWinProb: 0.31,
    predHomeScore: 75,
    predAwayScore: 83,
    isProvisional: false,
    isFinal: false,
    isEarlySeason: false,
  },
];

/** `status = 'SUCCESS'` の最新から24時間以上経過しているか（基本設計 4.5） */
export const SAMPLE_STALE: { stale: boolean; generatedAtLabel: string } = {
  stale: false,
  generatedAtLabel: '9月22日 6:02',
};

/**
 * **予測がまだ生成されていない試合**（要件 8.5 の空状態）。
 *
 * 予測は試合前日の朝に出るため、日程に載っているのに予測がない試合が実在する。
 * 「試合そのものがない（404）」と「試合はあるが予測がない」を混ぜないため、
 * 別の一覧として持つ。11b では API の `prediction` が null の応答がこれに当たる。
 */
export const SAMPLE_PENDING_GAMES: {
  gameId: string;
  tipoffLabel: string | null;
  home: Club;
  away: Club;
}[] = [
  {
    gameId: 'demo-pending',
    tipoffLabel: '19:05',
    home: { slug: 'demo-gulls', name: '架空ゴールズ', shortName: '架空G' },
    away: { slug: 'demo-hawks', name: '架空ホークス', shortName: '架空H' },
  },
];

/**
 * **当日13試合の日**（1日の最大構成）。B.PREMIER は26クラブで、全クラブが
 * 同じ日に試合をすると13試合になる。1試合だけを大きく出す構成が、この規模でも
 * 読めるかを確かめるために置く。
 *
 * **開始時刻の昇順に並べる。** 実データも同じ順で渡す（下記 `byTipoff`）。
 */
export const SAMPLE_FULL_DAY: GameView[] = Array.from({ length: 13 }, (_, index) => {
  const hours = ['14:05', '15:05', '16:05', '17:05', '18:05', '19:05'];
  const probs = [0.68, 0.55, 0.31, 0.5, 0.74, 0.46, 0.62, 0.38, 0.81, 0.53, 0.29, 0.66, 0.44];
  const names = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'.split('');
  const homeLetter = names[index * 2] ?? 'X';
  const awayLetter = names[index * 2 + 1] ?? 'Y';
  return {
    gameId: `demo-full-${index + 1}`,
    tipoffLabel: hours[index % hours.length] ?? null,
    status: 'SCHEDULED' as const,
    // **リンク先が無い slug を作らない。** 13試合ぶんのクラブ別ページは
    // 生成していないため、既にある slug を使い回す（11a は合成データ）
    home: { slug: 'demo-alphas', name: `架空${homeLetter}クラブ`, shortName: `架空${homeLetter}` },
    away: { slug: 'demo-bulls', name: `架空${awayLetter}クラブ`, shortName: `架空${awayLetter}` },
    homeWinProb: probs[index] ?? 0.5,
    predHomeScore: 78 + (index % 5) * 3,
    predAwayScore: 75 + ((index + 2) % 6) * 3,
    isProvisional: index % 3 === 0,
    isFinal: false,
    isEarlySeason: false,
  };
});
