// **表示確認用の合成データ。** クラブ名も選手名も架空である（要件 4.5.2）。
// 静的JSON のスキーマ（U-09）はここで決めない。
import type { HistoryView } from '@/components/prediction/HistoryRow';
import type { ResultView } from '@/components/prediction/ResultComparison';

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

/**
 * **試合カードから辿れるクラブすべてに用意する。** 1件だけ作って他を 404 にすると、
 * 「リンクがあるのに開けない」状態になる（基本設計 5.1 は
 * `/games/[id] → /teams/[slug]` を定めている）。
 */
const SEEDS: [slug: string, name: string, elo: number][] = [
  ['demo-alphas', '架空アルファーズ', 1582],
  ['demo-bulls', '架空ブルズ', 1494],
  ['demo-cats', '架空キャッツ', 1531],
  ['demo-dolphins', '架空ドルフィンズ', 1448],
  ['demo-eagles', '架空イーグルス', 1387],
  ['demo-foxes', '架空フォックス', 1620],
  ['demo-gulls', '架空ゴールズ', 1503],
  ['demo-hawks', '架空ホークス', 1466],
];

export const SAMPLE_TEAMS: Record<string, TeamView> = Object.fromEntries(
  SEEDS.map(([slug, name, elo], index) => [
    slug,
    {
      slug,
      name,
      // 見た目の確認用に少しずつ変える。**強いクラブほど勝っている形にする**
      last5: (['W', 'W', 'L', 'W', 'W'] as ('W' | 'L')[]).map((value, position) =>
        (index + position) % 4 === 0 ? 'L' : value,
      ),
      avgMargin: Number(((elo - 1500) / 25).toFixed(1)),
      elo,
      record: { wins: 10 + (index % 5), losses: 10 - (index % 5) },
      accuracy: { rate: 0.6 + (index % 4) * 0.05, n: 20 },
    },
  ]),
);

export const SAMPLE_TEAM: TeamView = SAMPLE_TEAMS['demo-alphas'] as TeamView;

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

/**
 * 予測履歴の各試合を**試合後の試合詳細**として開けるようにする。
 *
 * **リンク先のない行を作らない。** 履歴の行は `/games/[id]` へ送るため、
 * 対応する試合詳細が生成されていなければ 404 になる
 * （`npm run test:links` が検出する）。
 */
export const SAMPLE_HISTORY_RESULTS: ResultView[] = SAMPLE_HISTORY.map((item) => {
  const own = item.ownWinProb;
  // 履歴は「このクラブから見た」値なので、ホーム視点へ戻す
  const homeWinProb = item.isHome ? own : 1 - own;
  const homeScore = item.isHome ? item.ownScore : item.opponentScore;
  const awayScore = item.isHome ? item.opponentScore : item.ownScore;
  const lower = Math.floor(Math.max(homeWinProb, 1 - homeWinProb) * 10) * 10;
  return {
    gameId: item.gameId,
    home: { name: item.isHome ? SAMPLE_TEAM.name : item.opponentName },
    away: { name: item.isHome ? item.opponentName : SAMPLE_TEAM.name },
    homeScore,
    awayScore,
    homeWinProb,
    // 予想スコアは整数（要件 8.3）
    predHomeScore: Math.round(homeScore - 4),
    predAwayScore: Math.round(awayScore + 2),
    isCorrect: item.isCorrect,
    scoreError: Math.abs(homeScore - awayScore - (homeScore - 4 - (awayScore + 2))),
    bucket: { label: `${lower}-${lower + 10}%`, n: 42, correct: 29, rate: 29 / 42 },
  };
});
