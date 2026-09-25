import Link from 'next/link';
import { ProbabilityBar } from '@/components/prediction/ProbabilityBar';
import { StatusBadge } from '@/components/prediction/StatusBadge';
import { statusBadgeKind, type AccuracyView, type GameView } from '@/lib/view';

/** 試合1件は article。対戦名を見出しにする（視覚的に隠してよい。詳細設計 5.2） */
export function GameCard({ game, accuracy }: { game: GameView; accuracy: AccuracyView }) {
  const kind = statusBadgeKind(game);

  return (
    <article className="card-shadow rounded-2xl border border-border bg-surface p-4">
      <h2 className="sr-only">
        {game.home.name} 対 {game.away.name}
      </h2>
      <div className="flex items-center justify-between text-[11px] font-bold tracking-wider text-text-2">
        <span>B.PREMIER{game.tipoffLabel ? ` ${game.tipoffLabel}` : ' 時刻未定'}</span>
        <span className="flex gap-1">
          {game.isEarlySeason && <StatusBadge kind="early" />}
          {/* 開始前に「確定」を出さない。該当しなければ何も出さない（lib/view.ts） */}
          {kind && <StatusBadge kind={kind} />}
        </span>
      </div>

      <div className="mt-2 flex items-baseline justify-between gap-2 text-[14px] font-bold">
        <span className="min-w-0 truncate">{game.home.name}</span>
        <span className="min-w-0 truncate text-right text-text-2">{game.away.name}</span>
      </div>

      <div className="mt-2">
        <ProbabilityBar game={game} />
      </div>

      <dl className="mt-3 flex items-baseline justify-between">
        <dt className="text-[11px] font-bold tracking-wider text-text-2">予想スコア</dt>
        {/* 予想スコアは整数（要件 8.3） */}
        <dd className="text-[28px] font-extrabold">
          {game.predHomeScore} <span className="text-text-3">–</span> {game.predAwayScore}
        </dd>
      </dl>

      {/* 的中率には母数を併記する（要件 8.3） */}
      <p className="mt-1 text-[12px] text-text-3">
        今季的中率 {(accuracy.rate * 100).toFixed(1)}%（{accuracy.n}試合）
      </p>

      <Link
        href={`/games/${game.gameId}/`}
        className="mt-3 flex min-h-11 items-center justify-center rounded-xl border border-border text-[13px] font-bold text-text"
      >
        根拠と個人スタッツを見る
      </Link>
    </article>
  );
}
