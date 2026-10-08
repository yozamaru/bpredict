// 契約ファイルの形 → 画面が受け取る値（`lib/view.ts`）。
//
// **1か所に置く。** 静的JSON と公開API は同じ形であり（詳細設計 3.7）、
// 取得先ごとに変換を書くと**片方だけ直したときに画面が静かに壊れる**。

import type {
  AccuracyShape,
  ClubShape,
  FactorShape,
  GameShape,
  GamesByDate,
  Meta,
  ReasonShape,
  ResultsByDate,
} from '@/lib/source';
import type { ResultView } from '@/components/prediction/ResultComparison';
import type {
  AccuracyView, ActualView, Club, FactorView, GameView, PlayerView, ReasonView,
} from '@/lib/view';

/** 遅延と判定する間隔（時間。基本設計 4.5）。 */
export const STALE_HOURS = 24;

/**
 * UTC の時刻を JST の「HH:MM」にする（CLAUDE.md 時刻の扱い）。
 *
 * **サーバに作らせない。** `game_date` の定義（その試合の JST における暦日）と
 * タイムゾーン変換が2箇所に分かれる（詳細設計 3.3）。
 *
 * **固定のオフセットで足す。** `Intl` に頼ると実行環境のタイムゾーンデータに
 * 依存し、同じ入力が端末で違う結果になりうる。日本には夏時間がない。
 */
export function tipoffLabel(tipoffAt: string | null): string | null {
  if (tipoffAt === null) return null;
  const parsed = Date.parse(tipoffAt);
  if (Number.isNaN(parsed)) return null;
  const jst = new Date(parsed + 9 * 60 * 60 * 1000);
  const hours = String(jst.getUTCHours()).padStart(2, '0');
  const minutes = String(jst.getUTCMinutes()).padStart(2, '0');
  return `${hours}:${minutes}`;
}

/** `YYYY-MM-DD` を「9月22日（火）」にする。**曜日も JST で決める。** */
export function dateLabel(date: string): string {
  const parsed = Date.parse(`${date}T00:00:00Z`);
  if (Number.isNaN(parsed)) return date;
  const day = new Date(parsed);
  const names = ['日', '月', '火', '水', '木', '金', '土'];
  return `${day.getUTCMonth() + 1}月${day.getUTCDate()}日（${names[day.getUTCDay()]}）`;
}

/** `YYYY-MM-DD` に日を足す。**日付の導線に使う**（前日・翌日）。 */
export function shiftDate(date: string, days: number): string {
  const parsed = Date.parse(`${date}T00:00:00Z`);
  if (Number.isNaN(parsed)) return date;
  return new Date(parsed + days * 86_400_000).toISOString().slice(0, 10);
}

/**
 * クラブ。**表示名が null のときは slug を出す**（詳細設計 4.2 のステップ5）。
 *
 * 当季の `club_seasons` は最初の試合が終わるまで空であり、`name` が null になる。
 * **`clubs.name`（現在の表示名）で埋めない** — 過去試合の表示が遡って変わる。
 */
export function toClub(shape: ClubShape): Club {
  return {
    clubId: shape.clubId,
    slug: shape.slug,
    name: shape.name ?? shape.slug,
    shortName: shape.shortName ?? shape.name ?? shape.slug,
  };
}

/**
 * 1試合。**予測がない試合は `null` を返す側で扱う**（要件 8.5 の「予測未生成」）。
 *
 * **予想スコアは整数に丸める**（要件 8.3。MAE 8〜10点に対し小数第1位は精度の
 * 誤認を招く）。丸めは画面の仕事であり、保存される値は丸めない（詳細設計 4.2）。
 */
export function toGame(shape: GameShape): GameView | null {
  if (shape.prediction === null) return null;
  const prediction = shape.prediction;
  return {
    gameId: shape.gameId,
    tipoffLabel: tipoffLabel(shape.tipoffAt),
    status: asStatus(shape.status),
    home: toClub(shape.home),
    away: toClub(shape.away),
    homeWinProb: prediction.homeWinProb,
    predHomeScore: Math.round(prediction.predHomeScore),
    predAwayScore: Math.round(prediction.predAwayScore),
    isProvisional: prediction.isProvisional,
    isFinal: prediction.isFinal,
    // **null は false として扱う。** 「序盤である」と主張できないため出さない
    isEarlySeason: prediction.isEarlySeason === true,
    // **実績はサーバが出した値をそのまま写す**（v1.131）。終了していなければ null で
    // あり、**スコアから勝敗を計算し直さない**（`VOID` の扱いが画面側へ漏れる）
    homeScore: shape.homeScore ?? null,
    awayScore: shape.awayScore ?? null,
    // **null は「まだ照合していない」であって「外した」ではない**（3.3）
    isCorrect: shape.evaluation?.isCorrect ?? null,
    scoreError: shape.evaluation?.scoreError ?? null,
  };
}

function asStatus(value: string): GameView['status'] {
  return value === 'FINISHED' || value === 'POSTPONED' || value === 'CANCELLED'
    ? value
    : 'SCHEDULED';
}

/** 予測がある試合だけを返す。**件数の差は呼び出し側が注記に使う。** */
export function toGames(list: GamesByDate): GameView[] {
  return list.games
    .map(toGame)
    .filter((game): game is GameView => game !== null);
}

/** 的中率。**母数が 0 のときは出さない**（母数の併記が要件 8.3 の条件である）。 */
export function toAccuracy(shape: AccuracyShape, baselineRate: number | null): AccuracyView | null {
  if (shape === null || shape.n <= 0 || baselineRate === null) return null;
  return { rate: shape.accuracy, baselineRate, n: shape.n };
}

/** 根拠。**`strength` は 1〜4 の段階値で、寄与の生値は来ない**（詳細設計 2.7）。 */
export function toReason(shape: ReasonShape): ReasonView {
  return {
    label: shape.label,
    value: shape.value,
    favors: shape.favors === 'AWAY' ? 'AWAY' : 'HOME',
    strength: toStrength(shape.strength),
  };
}

/** 範囲外の値を黙って通さない。**API は 1〜4 を返す契約である**（詳細設計 3.3）。 */
function toStrength(value: number): ReasonView['strength'] {
  const rounded = Math.round(value);
  if (rounded <= 1) return 1;
  if (rounded === 2) return 2;
  if (rounded === 3) return 3;
  return 4;
}

/**
 * 更新遅延の判定（基本設計 4.5 / 詳細設計 3.7）。
 *
 * **`meta.json` に真偽値は入っていない。** バッチは自分が書いた時点しか知らず、
 * 「今」から24時間経ったかを判定できない。**判定はここで行う。**
 *
 * **`lastSuccessAt` を見る**（`generatedAt` ではない）。毎日失敗しても
 * `generatedAt` は毎日新しくなるため、遅延と判定されない。
 */
export function isStale(meta: Meta, now: number = Date.now()): boolean {
  if (meta.lastSuccessAt === null) return false;
  const last = Date.parse(meta.lastSuccessAt);
  if (Number.isNaN(last)) return false;
  return now - last > STALE_HOURS * 60 * 60 * 1000;
}

/** 「9月26日 6:02時点」。**JST で出す**（要件 8.4 の文言）。 */
export function generatedAtLabel(generatedAt: string): string {
  const parsed = Date.parse(generatedAt);
  if (Number.isNaN(parsed)) return generatedAt;
  const jst = new Date(parsed + 9 * 60 * 60 * 1000);
  return (
    `${jst.getUTCMonth() + 1}月${jst.getUTCDate()}日 ` +
    `${jst.getUTCHours()}:${String(jst.getUTCMinutes()).padStart(2, '0')}`
  );
}

// --- 的中率ページ（工程14。基本設計 5.2 / 詳細設計 3.3 の `/accuracy`） ---

/**
 * 的中した試合数。**API は返さないので導く**（`accuracy_summary` は率と母数だけ）。
 *
 * 基本設計 5.2 の文言「312試合中 223試合を的中（71.4%）」が件数を要求している。
 * `accuracy = 的中数 ÷ n` を倍し直すだけで、REAL（倍精度）に収まる範囲では
 * 元の整数に戻る。
 */
export function correctCount(accuracy: number, n: number): number {
  return Math.round(accuracy * n);
}

/**
 * 較正の言い換え（基本設計 5.2 の3番目）。**専門指標の前に置く一文。**
 *
 * **「やや」のような大きさの形容を入れない。** 大きさを言うには閾値が要り、
 * それは設計文書にない定数になる（CLAUDE.md「勝手な仕様補完をしない」）。
 * 代わりに**開きをポイントで出す** — 読者が自分で大きさを判断できる。
 *
 * **開きは「ポイント」で書く。** `%` は勝率専用に予約されており（要件 8.3）、
 * 2つの率の差に付けると「勝率が1.4%」と誤読される（詳細設計 2.7.1 と同じ規則）。
 */
export function calibrationNote(predicted: number, actual: number): string {
  const gap = (actual - predicted) * 100;
  // **表示する桁（小数第1位）で 0.0 になる開きは「予想どおり」とする。**
  // これは精度の閾値ではなく丸めの帰結である — `0.0ポイント下回っています` と
  // 出すのは、開きが無いと言いながら向きを主張することになる
  if (Math.abs(gap) < 0.05) return '予想どおりです。';
  const amount = Math.abs(gap).toFixed(1);
  return gap < 0
    ? `予想を ${amount}ポイント下回っています。`
    : `予想を ${amount}ポイント上回っています。`;
}

/** 試合詳細の `playerPredictions[]`（詳細設計 3.3 / 3.7）。 */
type RawShooting = { m: number; a: number; pct: number | null };
type RawPlayer = {
  playerId: string;
  name: string;
  position: string | null;
  clubId: string;
  availProb: number;
  summary: { min: number; pts: number; reb: number; ast: number };
  error: {
    min: number | null;
    pts: number | null;
    reb: number | null;
    ast: number | null;
  };
  box: {
    fg: RawShooting;
    fg2: RawShooting;
    fg3: RawShooting;
    ft: RawShooting;
    oreb: number;
    dreb: number;
    ast: number;
    tov: number;
    stl: number;
    blk: number;
    pf: number;
    fd: number;
    efgPct: number | null;
    tsPct: number | null;
  };
};

const POSITIONS = ['PG', 'SG', 'SF', 'PF', 'C'] as const;

/**
 * ポジション。**5値のどれでもなければ null** にする。
 *
 * 取り込みは5値のどれでもない値を落とすが（詳細設計 1.2）、**画面はサーバを
 * 信じきらない** — 列挙にない値をそのまま出すと、型が嘘になる。
 */
function asPosition(value: string | null): PlayerView['position'] {
  if (value === null) return null;
  return (POSITIONS as readonly string[]).includes(value)
    ? (value as PlayerView['position'])
    : null;
}

/**
 * **導出はサーバが済ませている。** `summary` / `box` をそのまま写すだけで、
 * 得点・リバウンド・率をここで計算しない（ui-implementation スキル）。
 * クライアントで計算すると実装ごとにずれる。
 *
 * **`pct` が null は「試投数が閾値未満で率を出さない」**（詳細設計 3.3 の閾値表）。
 * 閾値の判定もサーバ側にあり、ここでは判定しない。
 */
export function toPlayer(raw: RawPlayer): PlayerView {
  const box = raw.box;
  return {
    playerId: raw.playerId,
    name: raw.name,
    position: asPosition(raw.position),
    clubId: raw.clubId,
    availProb: raw.availProb,
    minutes: raw.summary.min,
    fg2a: box.fg2.a,
    fg3a: box.fg3.a,
    fta: box.ft.a,
    fg2Pct: box.fg2.a > 0 ? box.fg2.m / box.fg2.a : 0,
    fg3Pct: box.fg3.a > 0 ? box.fg3.m / box.fg3.a : 0,
    ftPct: box.ft.a > 0 ? box.ft.m / box.ft.a : 0,
    oreb: box.oreb,
    dreb: box.dreb,
    ast: box.ast,
    tov: box.tov,
    stl: box.stl,
    blk: box.blk,
    pf: box.pf,
    fd: box.fd,
    err: {
      minutes: raw.error.min,
      pts: raw.error.pts,
      reb: raw.error.reb,
      ast: raw.error.ast,
    },
    derived: {
      pts: raw.summary.pts,
      reb: raw.summary.reb,
      fg: box.fg,
      fg2: box.fg2,
      fg3: box.fg3,
      ft: box.ft,
      efgPct: box.efgPct,
    },
  };
}

/**
 * 形が合わない行は落とす。**件数は呼び出し側が見る** — 画面は
 * 「0人なら出さない」だけで、何人落ちたかを主張しない。
 */
export function toPlayers(list: unknown[]): PlayerView[] {
  const out: PlayerView[] = [];
  for (const item of list) {
    const raw = item as Partial<RawPlayer>;
    if (
      typeof raw.playerId !== 'string'
      || raw.summary === undefined
      || raw.box === undefined
      || raw.error === undefined
    ) continue;
    out.push(toPlayer(raw as RawPlayer));
  }
  return out;
}

/**
 * `/results?date=` の応答を画面の形にする（詳細設計 3.3）。
 *
 * **判定と誤差と帯の通算はサーバが出した値を使う**（`ResultView` の注記）。
 * 画面でスコアから計算し直すと、VOID の扱いが画面側の実装に漏れる。
 *
 * **揃っていない行は落とす。** `evaluation` は `prediction_results` がある試合に
 * だけ付き、`bucketContext` が null の行は帯の通算を併記できない — 要件 8.3 は
 * 「その確率帯の通算的中率を併記する」と定めており、**併記できない行を出さない**。
 */
export function toResults(source: ResultsByDate): ResultView[] {
  const out: ResultView[] = [];
  for (const row of source.results) {
    const { prediction: p, evaluation: e } = row;
    if (
      p === null || e === null
      || row.homeScore === null || row.awayScore === null
      || p.predHomeScore === null || p.predAwayScore === null
      || e.isCorrect === null || e.scoreError === null
      || e.bucketContext === null
    ) continue;
    out.push({
      gameId: row.gameId,
      home: { name: toClub(row.home).name },
      away: { name: toClub(row.away).name },
      homeScore: row.homeScore,
      awayScore: row.awayScore,
      homeWinProb: p.homeWinProb,
      predHomeScore: p.predHomeScore,
      predAwayScore: p.predAwayScore,
      isCorrect: e.isCorrect,
      scoreError: e.scoreError,
      bucket: {
        label: e.bucketContext.bucket,
        n: e.bucketContext.n,
        correct: e.bucketContext.correct,
        rate: e.bucketContext.rate,
      },
    });
  }
  return out;
}

/** 試合詳細の `playerActuals[]`（詳細設計 3.3）。 */
type RawActualShooting = { m: number | null; a: number | null; pct: number | null };
type RawActual = {
  playerId: string;
  name: string;
  position: string | null;
  clubId: string;
  started: boolean | null;
  summary: {
    min: number | null;
    pts: number | null;
    reb: number | null;
    ast: number | null;
  };
  box: {
    fg: RawActualShooting;
    fg2: RawActualShooting;
    fg3: RawActualShooting;
    ft: RawActualShooting;
    oreb: number | null;
    dreb: number | null;
    ast: number | null;
    tov: number | null;
    stl: number | null;
    blk: number | null;
    pf: number | null;
    fd: number | null;
    plusMinus: number | null;
    efgPct: number | null;
    tsPct: number | null;
  };
};

/**
 * 実績。**サーバが出した値をそのまま写す**（予測と同じ作法）。
 *
 * **率を計算し直さない。** `pct` が null なのは「試投数が0」であって、
 * 予測側の閾値とは別の理由である（詳細設計 3.3）。
 */
export function toActual(raw: RawActual): ActualView {
  const box = raw.box;
  return {
    playerId: raw.playerId,
    name: raw.name,
    clubId: raw.clubId,
    position: asPosition(raw.position),
    started: raw.started,
    minutes: raw.summary.min,
    pts: raw.summary.pts,
    reb: raw.summary.reb,
    ast: raw.summary.ast,
    oreb: box.oreb,
    dreb: box.dreb,
    tov: box.tov,
    stl: box.stl,
    blk: box.blk,
    pf: box.pf,
    fd: box.fd,
    plusMinus: box.plusMinus,
    fg: box.fg,
    fg2: box.fg2,
    fg3: box.fg3,
    ft: box.ft,
    efgPct: box.efgPct,
  };
}

/** 形が合わない行は落とす（`toPlayers` と同じ作法）。 */
export function toActuals(list: unknown[]): ActualView[] {
  const out: ActualView[] = [];
  for (const item of list) {
    const raw = item as Partial<RawActual>;
    if (
      typeof raw.playerId !== 'string'
      || raw.summary === undefined
      || raw.box === undefined
    ) continue;
    out.push(toActual(raw as RawActual));
  }
  return out;
}


/** 要因グループの識別子。**知らない値はそのまま通す**（画面が言い換えを持つ）。 */
const GROUPS = ['TEAM_STRENGTH', 'SCHEDULE', 'PLAYER', 'VENUE'] as const;

/**
 * この予測に使った項目（詳細設計 2.7.2）。
 *
 * **並べ替えない。** サーバが `rank`（グループ順 → 列順）で固定している。
 * 寄与の大きさで並べ替えると「どれがどれだけ効いたか」を主張することになる。
 */
export function toFactor(shape: FactorShape): FactorView {
  return {
    group: (GROUPS as readonly string[]).includes(shape.group) ? shape.group : 'TEAM_STRENGTH',
    label: shape.label,
    value: shape.value,
    // **画面はサーバを信じきらない**（`asPosition` と同じ作法）
    larger: shape.larger === 'HOME' || shape.larger === 'AWAY' ? shape.larger : null,
  };
}

/** 形が合わない行は落とす。 */
export function toFactors(list: readonly FactorShape[] | undefined): FactorView[] {
  return (list ?? [])
    .filter((f) => typeof f?.label === 'string' && typeof f.value === 'string')
    .map(toFactor);
}
