import Link from 'next/link';
import { isTossUp, percentPair, type GameView } from '@/lib/view';

/**
 * 圧縮カードでも**両チームの勝率を省略しない**（要件 8.3）。
 * 片側だけだと誰の55%か判別できず、55%と81%が同じ見た目になる。
 */
export function GameCardCompact({ game }: { game: GameView }) {
  const { home, away } = percentPair(game.homeWinProb);
  const label =
    `ホーム ${game.home.name} の勝率${home}パーセント、` +
    `アウェイ ${game.away.name} の勝率${away}パーセント`;

  return (
    <article className="rounded-2xl border border-border bg-surface">
      <h2 className="sr-only">
        {game.home.name} 対 {game.away.name}
      </h2>
      <Link href={`/games/${game.gameId}/`} className="flex min-h-11 flex-col justify-center px-4 py-3">
        <div role="img" aria-label={label} className="flex items-baseline justify-between gap-2">
          <span aria-hidden="true" className="min-w-0 truncate text-[13px] font-bold">
            {game.home.shortName} <span className="text-[16px] font-extrabold">{home}%</span>
          </span>
          <span aria-hidden="true" className="min-w-0 truncate text-right text-[13px] font-bold text-text-2">
            <span className="text-[16px] font-extrabold">{away}%</span> {game.away.shortName}
          </span>
        </div>
        <div aria-hidden="true" className="mt-1.5 flex h-1.5 overflow-hidden rounded-full bg-track">
          <div className="h-full bg-accent" style={{ width: `${home}%` }} />
        </div>
        <p className="mt-1.5 text-[11px] text-text-3">
          {isTossUp(game.homeWinProb) ? 'ほぼ互角 ・ ' : ''}
          {game.tipoffLabel ?? '時刻未定'}
        </p>
      </Link>
    </article>
  );
}
