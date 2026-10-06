/**
 * 実績の集計から1試合平均とボックススコアを導出する（詳細設計 1.9 / 3.3）。
 *
 * **集計テーブルが持つのは合計だけである。** 平均を列として持たない — 導出値を
 * 冗長に持つと不整合の余地が生まれる（1.5 の `pred_pts` を持たない理由と同じ）。
 *
 * **クライアントで計算させない。** 実装ごとにずれる（3.3 の `summary` / `box` と
 * 同じ規則）。
 */

/** 集計テーブルの行（カウント14項目。`plus_minus` は持たない。1.9）。 */
export type StatCounts = {
  fg2m: number | null;
  fg2a: number | null;
  fg3m: number | null;
  fg3a: number | null;
  ftm: number | null;
  fta: number | null;
  oreb: number | null;
  dreb: number | null;
  ast: number | null;
  tov: number | null;
  stl: number | null;
  blk: number | null;
  pf: number | null;
  fd: number | null;
};

const n = (value: number | null | undefined): number => value ?? 0;

/** 1試合平均。小数第1位（画面に出る値。詳細設計 5.5）。 */
const per = (total: number, games: number): number | null =>
  games > 0 ? Math.round((total / games) * 10) / 10 : null;

/**
 * 成功率。**試投数が0のときだけ `null` を返す**（要件 8.3）。
 *
 * 実績に閾値を設けない — 3.3 の `pct` が閾値を持つのは**予測値**だからである。
 * **合計を合計で割る**（1試合ごとの率を平均しない。2.2 の `off_rating` と同じ規則）。
 */
const pct = (made: number, attempted: number): number | null =>
  attempted > 0 ? Math.round((made / attempted) * 1000) / 1000 : null;

const shots = (made: number, attempted: number, games: number) => ({
  m: per(made, games),
  a: per(attempted, games),
  pct: pct(made, attempted),
});

/** ボックススコアの集計（1試合平均 + 率）。 */
export function boxOf(row: StatCounts, games: number) {
  const fgm = n(row.fg2m) + n(row.fg3m);
  const fga = n(row.fg2a) + n(row.fg3a);
  return {
    fg: shots(fgm, fga, games),
    fg2: shots(n(row.fg2m), n(row.fg2a), games),
    fg3: shots(n(row.fg3m), n(row.fg3a), games),
    ft: shots(n(row.ftm), n(row.fta), games),
    oreb: per(n(row.oreb), games),
    dreb: per(n(row.dreb), games),
    ast: per(n(row.ast), games),
    tov: per(n(row.tov), games),
    stl: per(n(row.stl), games),
    blk: per(n(row.blk), games),
    pf: per(n(row.pf), games),
    fd: per(n(row.fd), games),
  };
}

export type PlayerStatRow = StatCounts & {
  scope: string;
  scope_key: string;
  club_id: string;
  games: number;
  games_started: number;
  minutes: number | null;
  pts: number | null;
};

/** 選手の1行 → API の形（詳細設計 3.3）。 */
export function playerStatOf(row: PlayerStatRow) {
  const games = row.games;
  return {
    games,
    gamesStarted: row.games_started,
    perGame: {
      minutes: per(n(row.minutes), games),
      pts: per(n(row.pts), games),
      reb: per(n(row.oreb) + n(row.dreb), games),
      ast: per(n(row.ast), games),
    },
    box: boxOf(row, games),
  };
}

export type TeamStatRow = StatCounts & {
  scope: string;
  scope_key: string;
  games: number;
  wins: number;
  points_for: number | null;
  points_against: number | null;
  stat_games: number;
};

/**
 * クラブの1行 → API の形。
 *
 * **母数が2つある**（`games` と `statGames`）。`team_game_stats` が欠ける試合が
 * 実在するため、ボックススコアは `statGames` で割る（詳細設計 1.9）。
 *
 * **`losses` は返す。** 列としては持たないが（導出できる冗長列）、画面に出る値を
 * クライアントに計算させない。
 */
export function teamStatOf(row: TeamStatRow) {
  const games = row.games;
  const pointsFor = n(row.points_for);
  const pointsAgainst = n(row.points_against);
  return {
    games,
    wins: row.wins,
    losses: games - row.wins,
    perGame: {
      pointsFor: per(pointsFor, games),
      pointsAgainst: per(pointsAgainst, games),
      margin: per(pointsFor - pointsAgainst, games),
    },
    box: { statGames: row.stat_games, ...boxOf(row, row.stat_games) },
  };
}
