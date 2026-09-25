// **表示確認用の合成データ。** クラブ名も選手名も架空である（要件 4.5.2）。
// 静的JSON のスキーマ（U-09）はここで決めない。
import type { HistoryView } from '@/components/prediction/HistoryRow';

export type TeamView = {
  slug: string;
  /** その年度の表示名（`club_seasons.name` 相当） */
  name: string;
  /** 直近5試合。新しいものから */
  last5: ('W' | 'L')[];
  avgMargin: number;
  elo: number;
  /** 今季の消化数と勝敗 */
  record: { wins: number; losses: number };
  /** このクラブの試合に対する的中率。**母数を必ず併記する**（要件 8.3） */
  accuracy: { rate: number; n: number };
};

export const SAMPLE_TEAM: TeamView = {
  slug: 'demo-alphas',
  name: '架空アルファーズ',
  last5: ['W', 'W', 'L', 'W', 'W'],
  avgMargin: 6.2,
  elo: 1582,
  record: { wins: 14, losses: 6 },
  accuracy: { rate: 0.7, n: 20 },
};

/** **外れた試合を隠さない**（要件 8.3）。的中と外しの両方を置く */
export const SAMPLE_HISTORY: HistoryView[] = [
  {
    gameId: 'demo-h1',
    dateLabel: '9月20日',
    isHome: true,
    opponentName: '架空ブルズ',
    ownWinProb: 0.68,
    ownScore: 88,
    opponentScore: 81,
    isCorrect: true,
  },
  {
    gameId: 'demo-h2',
    dateLabel: '9月14日',
    isHome: false,
    opponentName: '架空キャッツ',
    ownWinProb: 0.44,
    ownScore: 79,
    opponentScore: 92,
    isCorrect: true,
  },
  {
    gameId: 'demo-h3',
    dateLabel: '9月13日',
    isHome: false,
    opponentName: '架空キャッツ',
    ownWinProb: 0.52,
    ownScore: 90,
    opponentScore: 85,
    isCorrect: true,
  },
  {
    gameId: 'demo-h4',
    dateLabel: '9月7日',
    isHome: true,
    opponentName: '架空ドルフィンズ',
    ownWinProb: 0.71,
    ownScore: 74,
    opponentScore: 80,
    isCorrect: false,
  },
  {
    gameId: 'demo-h5',
    dateLabel: '9月6日',
    isHome: true,
    opponentName: '架空ドルフィンズ',
    ownWinProb: 0.66,
    ownScore: 95,
    opponentScore: 77,
    isCorrect: true,
  },
];
