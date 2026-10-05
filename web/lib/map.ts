// 契約ファイルの形 → 画面が受け取る値（`lib/view.ts`）。
//
// **1か所に置く。** 静的JSON と公開API は同じ形であり（詳細設計 3.7）、
// 取得先ごとに変換を書くと**片方だけ直したときに画面が静かに壊れる**。

import type {
  AccuracyShape,
  ClubShape,
  GameShape,
  GamesByDate,
  Meta,
  ReasonShape,
} from '@/lib/source';
import type { AccuracyView, Club, GameView, ReasonView } from '@/lib/view';

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
