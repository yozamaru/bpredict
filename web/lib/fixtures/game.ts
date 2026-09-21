// **表示確認用の合成データ。** 実際の予測ではなく、選手名・クラブ名も架空である。
import type { PlayerView, ReasonView } from '@/lib/view';

export const SAMPLE_REASON_SUMMARY =
  'ホームのチーム力が上回っていること、アウェイが2連戦の2戦目で疲労していることが、この予測の主な理由です。';

export const SAMPLE_REASONS: ReasonView[] = [
  { label: 'チーム力の差', value: '＋82ポイント', favors: 'HOME', strength: 4 },
  { label: 'アウェイの休養', value: '中0日（ホームは中2日）', favors: 'HOME', strength: 3 },
  { label: 'アウェイの主力欠場', value: '平均28分の選手が1名', favors: 'HOME', strength: 2 },
  { label: '開催会場', value: '代替アリーナ', favors: 'AWAY', strength: 1 },
];

function player(
  playerId: string,
  name: string,
  position: PlayerView['position'],
  overrides: Partial<PlayerView>,
): PlayerView {
  return {
    playerId,
    name,
    position,
    availProb: 0.95,
    minutes: 24,
    fg2a: 6,
    fg3a: 3,
    fta: 2,
    fg2Pct: 0.52,
    fg3Pct: 0.35,
    ftPct: 0.78,
    oreb: 1,
    dreb: 3,
    ast: 2,
    tov: 1.5,
    stl: 0.8,
    blk: 0.3,
    pf: 2,
    fd: 2,
    err: { minutes: 5.8, pts: 4.8, reb: 2.1, ast: 1.5 },
    ...overrides,
  };
}

export const SAMPLE_PLAYERS: PlayerView[] = [
  player('p1', '架空 一郎', 'PG', {
    minutes: 31.2,
    fg2a: 7.8,
    fg3a: 5.3,
    fta: 3.9,
    fg2Pct: 0.526,
    fg3Pct: 0.434,
    ftPct: 0.846,
    oreb: 0.6,
    dreb: 2.5,
    ast: 6.1,
    tov: 2.2,
    stl: 1.1,
  }),
  player('p2', '架空 二郎', 'C', {
    minutes: 28.5,
    fg2a: 9.1,
    fg3a: 0.4,
    fta: 4.2,
    fg2Pct: 0.58,
    fg3Pct: 0.2,
    ftPct: 0.62,
    oreb: 3.1,
    dreb: 6.7,
    ast: 1.4,
    blk: 1.2,
    pf: 3.1,
    fd: 3.4,
  }),
  player('p3', '架空 三郎', 'SF', { minutes: 22.4, availProb: 0.72 }),
  // P(出場) < 0.5 は表示されないことの確認用
  player('p4', '架空 四郎', 'SG', { minutes: 8.1, availProb: 0.31 }),
];

export const SAMPLE_MODEL = { version: 'winner-v1.0.0', accuracy: 0.682, brier: 0.204, n: 312 };

export const SAMPLE_FORM = {
  home: { last5: ['W', 'W', 'L', 'W', 'W'], avgMargin: 6.2, elo: 1582 },
  away: { last5: ['L', 'W', 'L', 'L', 'W'], avgMargin: -1.4, elo: 1500 },
} as const;
