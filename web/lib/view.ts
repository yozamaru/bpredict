// 画面が受け取る値の形。**静的JSON のスキーマではない。**
// 静的JSON の全体像は未決事項 U-09 であり、工程9 で書き出す側が決める。
// ここで先に決めると、書き出す側がこの形に縛られる（詳細設計 9章 の 11a の注記）。

export type GameStatus = 'SCHEDULED' | 'FINISHED' | 'POSTPONED' | 'CANCELLED';

export type Club = {
  /** 表示名。その年度の名称（club_seasons.name 相当） */
  name: string;
  /** 圧縮表示用の短縮名（club_seasons.short_name 相当） */
  shortName: string;
};

export type GameView = {
  gameId: string;
  /** JST の開始時刻「HH:MM」。未公表なら null */
  tipoffLabel: string | null;
  status: GameStatus;
  home: Club;
  away: Club;
  /** ホーム勝率 0〜1。アウェイは 1 - この値（冗長に持たない） */
  homeWinProb: number;
  /** 予想スコアは整数で持つ。MAE 8〜10点に対し小数第1位は精度の誤認を招く */
  predHomeScore: number;
  predAwayScore: number;
  isProvisional: boolean;
  isFinal: boolean;
  /** 両チームとも消化5試合未満 */
  isEarlySeason: boolean;
};

export type AccuracyView = {
  /** 的中率 0〜1 */
  rate: number;
  /** 母数。必ず併記する（要件 8.3） */
  n: number;
};

/** 45〜55% は「ほぼ互角」と表現し、一方的な試合と見た目で区別する（要件 8.3） */
export function isTossUp(homeWinProb: number): boolean {
  return homeWinProb >= 0.45 && homeWinProb <= 0.55;
}

/** 表示は整数の百分率。丸め後も合計が100になるように片側から導く */
export function percentPair(homeWinProb: number): { home: number; away: number } {
  const home = Math.round(homeWinProb * 100);
  return { home, away: 100 - home };
}

export type ReasonView = {
  /** 表示名。生の特徴量名は出さない（要件 6.9） */
  label: string;
  value: string;
  favors: 'HOME' | 'AWAY';
  /** 1〜4 の段階値。SHAP の生値は返さない（詳細設計 3.3） */
  strength: 1 | 2 | 3 | 4;
};

/** 個人スタッツ予測。成功数は率×試投数の導出値で、独立に持たない（要件 6.8.2） */
export type PlayerView = {
  playerId: string;
  name: string;
  position: 'PG' | 'SG' | 'SF' | 'PF' | 'C';
  availProb: number;
  minutes: number;
  fg2a: number;
  fg3a: number;
  fta: number;
  fg2Pct: number;
  fg3Pct: number;
  ftPct: number;
  oreb: number;
  dreb: number;
  ast: number;
  tov: number;
  stl: number;
  blk: number;
  pf: number;
  fd: number;
  /** 誤差の目安は主要4項目のみ（要件 6.8.6） */
  err: { minutes: number; pts: number; reb: number; ast: number };
  /**
   * 導出値。**サーバが導出した値をそのまま表示する**（ui-implementation スキル）。
   * クライアントで計算すると実装ごとにずれる。11b では API の
   * `summary` / `box`（詳細設計 3.3）がこの位置に入る。
   * `pct` が null は「試投数が閾値未満で率を出さない」を意味する。
   */
  derived: {
    pts: number;
    reb: number;
    fg: { m: number; a: number; pct: number | null };
    fg2: { m: number; a: number; pct: number | null };
    fg3: { m: number; a: number; pct: number | null };
    ft: { m: number; a: number; pct: number | null };
    efgPct: number | null;
  };
};

/** 試投数が閾値未満なら率を出さない（詳細設計 3.3 の閾値表） */
export const PCT_THRESHOLD = { fg: 4, split: 3, ft: 3 } as const;

/**
 * 導出の規則。**画面からは呼ばない。** 11a では fixture（サーバ役）が使い、
 * 11b では API が同じ導出を行う。恒等式は要件 6.8.2 にある。
 */
export function derive(
  source: Omit<PlayerView, 'derived'>,
): PlayerView['derived'] {
  const fg2m = source.fg2Pct * source.fg2a;
  const fg3m = source.fg3Pct * source.fg3a;
  const ftm = source.ftPct * source.fta;
  const fga = source.fg2a + source.fg3a;
  const fgm = fg2m + fg3m;
  const show = (attempted: number, threshold: number, pct: number) =>
    attempted >= threshold ? pct : null;
  return {
    // 得点は恒等式で導出する。独立に予測しない
    pts: fg2m * 2 + fg3m * 3 + ftm,
    reb: source.oreb + source.dreb,
    fg: { m: fgm, a: fga, pct: show(fga, PCT_THRESHOLD.fg, fga > 0 ? fgm / fga : 0) },
    fg2: { m: fg2m, a: source.fg2a, pct: show(source.fg2a, PCT_THRESHOLD.split, source.fg2Pct) },
    fg3: { m: fg3m, a: source.fg3a, pct: show(source.fg3a, PCT_THRESHOLD.split, source.fg3Pct) },
    ft: { m: ftm, a: source.fta, pct: show(source.fta, PCT_THRESHOLD.ft, source.ftPct) },
    efgPct: show(fga, PCT_THRESHOLD.fg, fga > 0 ? (fgm + 0.5 * fg3m) / fga : 0),
  };
}

/**
 * 状態バッジの種類。**該当しなければ null で、何も出さない。**
 *
 * 「確定」は**試合開始をもって凍結された予測**を指す（CLAUDE.md 用語、要件 3.3）。
 * 開始前の予測は試合当日の再推論で変わりうるため、**開始前に「確定」と出しては
 * ならない**。出場者が公式に確定していても、それは「暫定が外れた」だけであって
 * 予測が凍結されたわけではない。
 *
 * 11a の実装は `isProvisional ? '暫定' : '確定'` になっており、**出場者が確定した
 * 開始前の試合に「確定」を出していた**。ユーザーは「もう変わらない」と読むため、
 * 用語の定義と食い違う。
 */
export function statusBadgeKind(game: GameView): 'provisional' | 'final' | null {
  if (game.isFinal) return 'final';
  if (game.isProvisional) return 'provisional';
  return null;
}
