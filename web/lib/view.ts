// 画面が受け取る値の形。**静的JSON のスキーマではない。**
// 静的JSON の全体像は未決事項 U-09 であり、工程9 で書き出す側が決める。
// ここで先に決めると、書き出す側がこの形に縛られる（詳細設計 9章 の 11a の注記）。

export type GameStatus = 'SCHEDULED' | 'FINISHED' | 'POSTPONED' | 'CANCELLED';

export type Club = {
  /**
   * クラブの識別子（詳細設計 3.3 の `clubId`）。**`slug` とは別物**で、
   * `playerPredictions[].clubId` と突き合わせるのに要る。
   *
   * **これも落としていた**（2026-10-08）。`toClub` が `slug` / `name` /
   * `shortName` だけを写しており、そのため個人スタッツをクラブ別に畳めなかった。
   */
  clubId: string;
  /**
   * `/teams/[slug]` の識別子。**改称があっても変わらない**（詳細設計 1.1）。
   * 表示名は年度で変わるが slug は恒久であり、API の応答にも含まれる（詳細設計 3.3）。
   */
  slug: string;
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
  /**
   * ベースライン「ホームが必ず勝つ」の的中率 0〜1。
   *
   * **同じ大きさで隣に置く**（要件 8.3 / 基本設計 5.2）。自分の成績だけを見せない。
   * 実測はリーグ戦4シーズン（n=1,829）で 52.7%（要件 付録B）。
   */
  baselineRate: number;
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

/**
 * 50% からの隔たり（ポイント）。中央基準バーが伸ばす量そのもの（詳細設計 5.3）。
 *
 * **表示の百分率から導く。** バーの長さと画面の数値がずれないようにするため、
 * 生の確率ではなく `percentPair` の丸め後の値を使う。
 */
export function deviation(homeWinProb: number): number {
  return Math.abs(percentPair(homeWinProb).home - 50);
}

/**
 * 優勢な側。バーはこちらへ伸びる。
 *
 * **ちょうど 50 対 50 のときはホーム側に置く。** 隔たりが 0 なので描かれる幅は
 * 最小値（2px）で、向きは見えない。どちらに置くかは表示に影響しないが、
 * 実装ごとに揺れないよう固定する。
 */
export function favoredSide(homeWinProb: number): 'home' | 'away' {
  return percentPair(homeWinProb).home >= 50 ? 'home' : 'away';
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
  /**
   * どのクラブの選手か（詳細設計 3.3 の `playerPredictions[].clubId`）。
   *
   * **落としていた**（2026-10-08 に気づいた）。API も契約（`contracts/public-shapes.json`）も
   * この値を持っているのに、`map.ts` が写していなかった。**そのため合計行を出せず、
   * 詳細設計 5.3 の「選手の合計が予想スコアと一致していることを画面で確かめられる」が
   * 未達だった** — クラブ別に畳めないため、合計すると 400分・両チーム得点の和という
   * 誤った数字になる。
   */
  clubId: string;
  /**
   * **null を許す。** 本番のロスターにはポジション未登録の選手が実在する
   * （2026-27 のクラブ 712 に1名。詳細設計 1.2）。**落とすと登録選手が
   * 候補一覧から消える**ため、NULL で残すのが設計の判断である。
   */
  position: 'PG' | 'SG' | 'SF' | 'PF' | 'C' | null;
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
  /**
   * 誤差の目安は主要4項目のみ（要件 6.8.6）。
   *
   * **null を許す。** 定義は「当該選手の直近N試合の絶対誤差の中央値」だが、
   * **`N` が未定義で、過去の個人予測と実績の対比が1件もない**（詳細設計 2.3.1）。
   * **0 を入れない** — 0 は「誤差がない」という意味を持ってしまう。
   */
  err: {
    minutes: number | null;
    pts: number | null;
    reb: number | null;
    ast: number | null;
  };
  /**
   * 導出値。**サーバが導出した値をそのまま表示する**（ui-implementation スキル）。
   * クライアントで計算すると実装ごとにずれる。**ここに入るのは API と静的JSON の
   * `summary` / `box`（詳細設計 3.3 / 3.7）である。**
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

/**
 * 開始時刻の昇順。**時刻未公表は末尾**に置く。
 *
 * 一覧は日程であり、**時刻順でないと読めない**。11a の fixture は
 * 19:05 → 17:05 → 14:05 の順に並んでいて、画面がその順で出していた。
 * 並べ替えを画面側に持たせるのは、静的JSON の並びに画面が依存しないようにするため
 * （書き出す側の順序が変わっても表示は崩れない）。
 */
export function byTipoff(a: GameView, b: GameView): number {
  if (a.tipoffLabel === b.tipoffLabel) return 0;
  if (a.tipoffLabel === null) return 1;
  if (b.tipoffLabel === null) return -1;
  return a.tipoffLabel < b.tipoffLabel ? -1 : 1;
}
