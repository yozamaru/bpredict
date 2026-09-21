// **表示確認用の合成データ。** 外れた試合を隠さないことを確認するため、
// 的中した試合と外した試合の両方を置く（要件 8.3）。
import type { ResultView } from '@/components/prediction/ResultComparison';

export const SAMPLE_RESULTS: ResultView[] = [
  {
    gameId: 'demo-r1',
    home: { name: '架空アルファーズ' },
    away: { name: '架空ブルズ' },
    homeScore: 88,
    awayScore: 81,
    homeWinProb: 0.68,
    predHomeScore: 84,
    predAwayScore: 78,
    isCorrect: true,
    scoreError: 3,
    bucket: { label: '60-70%', n: 42, correct: 29, rate: 29 / 42 },
  },
  {
    gameId: 'demo-r2',
    home: { name: '架空キャッツ' },
    away: { name: '架空ドルフィンズ' },
    homeScore: 74,
    awayScore: 86,
    homeWinProb: 0.71,
    predHomeScore: 83,
    predAwayScore: 77,
    isCorrect: false,
    scoreError: 18,
    bucket: { label: '70-80%', n: 81, correct: 62, rate: 62 / 81 },
  },
];
