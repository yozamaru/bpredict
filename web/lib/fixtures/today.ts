// **表示確認用の合成データ。** 実サイトから取得した値ではなく、クラブ名も架空である。
// 工程11a はデザインシステムと画面の骨組みだけを扱い、実データへの結線は 11b。
// 静的JSON のスキーマ（U-09）はここで決めない（詳細設計 9章）。
import type { AccuracyView, Club, GameView } from '@/lib/view';

export const SAMPLE_DATE_LABEL = '9月22日（火）';

export const SAMPLE_ACCURACY: AccuracyView = { rate: 0.682, n: 312 };

export const SAMPLE_GAMES: GameView[] = [
  {
    gameId: 'demo-1',
    tipoffLabel: '19:05',
    status: 'SCHEDULED',
    home: { name: '架空アルファーズ', shortName: '架空A' },
    away: { name: '架空ブルズ', shortName: '架空B' },
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
    home: { name: '架空キャッツ', shortName: '架空C' },
    away: { name: '架空ドルフィンズ', shortName: '架空D' },
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
    home: { name: '架空イーグルス', shortName: '架空E' },
    away: { name: '架空フォックス', shortName: '架空F' },
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
    home: { name: '架空ゴールズ', shortName: '架空G' },
    away: { name: '架空ホークス', shortName: '架空H' },
  },
];
