import { notFound } from 'next/navigation';
import { ProbabilityBar } from '@/components/prediction/ProbabilityBar';
import { ReasonList } from '@/components/prediction/ReasonList';
import { PlayerStatTable } from '@/components/prediction/PlayerStatTable';
import { StatusBadge } from '@/components/prediction/StatusBadge';
import {
  SAMPLE_FORM,
  SAMPLE_MODEL,
  SAMPLE_PLAYERS,
  SAMPLE_REASONS,
  SAMPLE_REASON_SUMMARY,
} from '@/lib/fixtures/game';
import { SAMPLE_GAMES } from '@/lib/fixtures/today';

export const dynamic = 'force-static';

// 静的生成の範囲は直近5シーズンに限る（要件 8.2）。11a は合成データの3件だけ。
export function generateStaticParams() {
  return SAMPLE_GAMES.map((game) => ({ id: game.gameId }));
}

// Next.js 16 では params が Promise（CLAUDE.md）
export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const game = SAMPLE_GAMES.find((candidate) => candidate.gameId === id);
  if (!game) notFound();

  return (
    <>
      <h2 className="mt-5 text-[19px] font-extrabold">
        {game.home.name} <span className="text-text-2">対</span> {game.away.name}
      </h2>
      <p className="mt-1 flex items-center gap-2 text-[11px] font-bold tracking-wider text-text-2">
        <span>B.PREMIER{game.tipoffLabel ? ` ${game.tipoffLabel}` : ' 時刻未定'}</span>
        {game.isEarlySeason && <StatusBadge kind="early" />}
        <StatusBadge kind={game.isProvisional ? 'provisional' : 'final'} />
      </p>

      <div className="mt-4 rounded-2xl border border-border bg-surface p-4">
        <ProbabilityBar game={game} />
        <dl className="mt-3 flex items-baseline justify-between">
          <dt className="text-[11px] font-bold tracking-wider text-text-2">予想スコア</dt>
          <dd className="text-[28px] font-extrabold">
            {game.predHomeScore} <span className="text-text-3">–</span> {game.predAwayScore}
          </dd>
        </dl>
      </div>

      {/* 状態の理由を一文で示す（要件 8.4）。具体時刻は U-06 が決まるまで書かない */}
      {game.isProvisional && (
        <p className="mt-3 text-[12px] leading-relaxed text-text-2">
          暫定 — 出場選手が未発表のため、直近5試合の出場傾向から推定しています。
          試合当日の午前中に更新されます。
        </p>
      )}
      {game.isEarlySeason && (
        <p className="mt-2 rounded-xl bg-warn-bg px-3 py-2 text-[12px] leading-relaxed text-warn">
          両チームとも今季4試合しか消化していないため、この予測はまだ精度が安定しません。
        </p>
      )}

      <ReasonList summary={SAMPLE_REASON_SUMMARY} reasons={SAMPLE_REASONS} />

      <PlayerStatTable players={SAMPLE_PLAYERS} />

      <section className="mt-6">
        <h3 className="text-[15px] font-extrabold">両チームの直近成績</h3>
        <dl className="mt-2 rounded-xl border border-border bg-surface p-3 text-[13px]">
          {(
            [
              ['home', game.home.name],
              ['away', game.away.name],
            ] as const
          ).map(([side, name]) => (
            <div key={side} className="flex items-baseline justify-between gap-2 py-0.5">
              <dt className="min-w-0 truncate text-text-2">{name}</dt>
              <dd>
                {SAMPLE_FORM[side].last5.join(' ')}
                <span className="text-text-2">
                  {' '}
                  ・ 平均得失点差 {SAMPLE_FORM[side].avgMargin > 0 ? '＋' : ''}
                  {SAMPLE_FORM[side].avgMargin.toFixed(1)}
                </span>
              </dd>
            </div>
          ))}
        </dl>
      </section>

      {/* 使用モデルと、そのモデルの通算精度（母数つき。要件 8.3） */}
      <p className="mt-4 text-[11px] leading-relaxed text-text-3">
        使用モデル {SAMPLE_MODEL.version} ・ 通算的中率{' '}
        {(SAMPLE_MODEL.accuracy * 100).toFixed(1)}%（{SAMPLE_MODEL.n}試合）
      </p>
    </>
  );
}
