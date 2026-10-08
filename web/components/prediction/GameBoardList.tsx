import Link from 'next/link';
import { AxisBar, WinProbRows } from '@/components/prediction/ProbabilityBar';
import { StatusBadge } from '@/components/prediction/StatusBadge';
import {
  favoredSide, isTossUp, percentPair, resultLine, statusBadgeKind, type GameView,
} from '@/lib/view';

/**
 * その日の試合を**1試合 = 1枚のボード**で並べる（基本設計 6.3。2026-10-08 に
 * スコアボード型を採用した）。
 *
 * **表（`<table>` ＋ `rowspan`）をやめた。** 旧版は1試合を2行のレコードに畳んで
 * 密度を上げていたが、**勝率が本文と同じ 12〜13px で、この画面の主役である数値が
 * 主役に見えなかった**。ボードにすると勝率を 40px で置けて、1枚が独立した
 * スコアボードとして読める。1試合あたり約180px。
 *
 * **区切りは面の明るさの差で作る**（基本設計 6.3）。線は意味を持つもの（50%軸）
 * だけに残し、ボードの境目は地（`--ground`）とボード（`--panel`）の差が示す。
 * 影は使わない。
 */
export function GameBoardList({ games, dateLabel }: { games: GameView[]; dateLabel: string }) {
  return (
    <section
      aria-label={`${dateLabel}の試合と予測`}
      className="mt-2.5 flex flex-col gap-2.5"
    >
      {games.map((game) => (
        <GameBoard key={game.gameId} game={game} />
      ))}
    </section>
  );
}

function GameBoard({ game }: { game: GameView }) {
  const { home, away } = percentPair(game.homeWinProb);
  const tossUp = isTossUp(game.homeWinProb);
  const kind = statusBadgeKind(game);
  // **実績が無ければ行を足さない**（詳細設計 5.3）。空の行はボードの高さだけを増やす
  const result = resultLine(game);
  const label =
    `ホーム ${game.home.name} の勝率${home}パーセント、` +
    `アウェイ ${game.away.name} の勝率${away}パーセント。` +
    (tossUp ? 'ほぼ互角。' : '') +
    `予想スコア ${game.predHomeScore}対${game.predAwayScore}`;

  return (
    <article>
      {/* 見出し要素で文書構造を作る（詳細設計 5.2）。対戦名は視覚的に隠してよい */}
      <h3 className="sr-only">
        {game.home.name} 対 {game.away.name}
      </h3>

      {/* **ボード全体が試合詳細へのタップ対象**（全幅 × 約180px）。44×44px を
          大きく上回る。リンクの名前は中身から組まれるため、時刻・状態・勝率・
          予想スコアがそのまま読み上げられる（勝率ブロックは一文に畳んである） */}
      <Link
        href={`/games/${game.gameId}/`}
        className="flex flex-col gap-2 rounded-[2px] bg-panel px-3.5 pb-3 pt-3"
      >
        <div className="flex items-center gap-2">
          <span className="num flex-1 text-[13px] tracking-[0.06em] text-ink-3">
            {game.tipoffLabel ?? '未定'}
          </span>
          {/* **序盤と暫定は両立する。** 旧版はどちらか一方しか出していなかった */}
          {game.isEarlySeason && <StatusBadge kind="early" />}
          {kind !== null && <StatusBadge kind={kind} />}
          {!game.isEarlySeason && kind === null && (
            <span className="num text-[12px] text-ink-3" aria-label="出場選手は発表済み">
              —
            </span>
          )}
          {/* ボードが開くことを示す。文言ではなく記号で示す */}
          <span aria-hidden="true" className="text-[14px] leading-none text-ink-3">
            ›
          </span>
        </div>

        <div role="img" aria-label={label} className="flex flex-col gap-2">
          <div aria-hidden="true">
            <WinProbRows game={game} size="list" />
          </div>

          {/* **縮尺は % にする。** 1ポイント = 1px では、確率がクランプの上限（95%）に
              寄ったとき隔たり45ポイント = 45px となり、器を越える。% ならバーの半分が
              50ポイントに対応し、構造的に収まる（詳細設計 5.3） */}
          <AxisBar
            homeWinProb={game.homeWinProb}
            className="h-3 w-full rounded-[1px]"
            bandHalf="5%"
            devScale="1%"
          />

          <div aria-hidden="true" className="flex items-baseline justify-between gap-2">
            {/* 優劣は色だけでなく**文字でも**示す（要件 8.3 / 8.6）。
                互角は塗りの札にして、一方的な試合と**形でも**区別する */}
            {tossUp ? (
              <span className="rounded-[2px] bg-strip px-1.5 py-0.5 text-[12px] font-bold text-ink-2">
                ほぼ互角
              </span>
            ) : (
              <span className="text-[12px] text-ink-3">
                {favoredSide(game.homeWinProb) === 'home' ? 'ホーム優勢' : 'アウェイ優勢'}
              </span>
            )}
            {/* 予想スコアは整数（要件 8.3）。平均誤差 8〜10点に対し小数第1位は
                精度の誤認を招く */}
            <span className="num text-[16px] text-ink">
              {game.predHomeScore} – {game.predAwayScore}
            </span>
          </div>
        </div>

        {/* **終了した試合は実績を下に1行足す**（詳細設計 5.3 の v1.131。
            運営者の指摘「詳細画面じゃないと結果が分からない」）。

            **予測の表示を変えない。** 上のブロックは終了前と同じままで、
            この行が増えるだけである（約180px → 約210px）。

            **判定が無くてもスコアは出す。** 終了したのに照合が付いていない状態は
            実在し（freeze は毎時、照合は日次）、そのとき出せるのはスコアだけである。

            **外れた試合に強い否定色を使わない**（基本設計 5.2）。文字で書く */}
        {result !== null && (
          <div className="mt-0.5 flex items-baseline justify-between gap-2 border-t border-rule pt-2">
            <span className="text-[12px] text-ink-3">
              実績
              {result.verdict !== null && (
                <span className="ml-1.5 text-ink-2">{result.verdict}</span>
              )}
            </span>
            <span className="num text-[16px] font-bold text-ink">{result.score}</span>
          </div>
        )}
      </Link>
    </article>
  );
}
