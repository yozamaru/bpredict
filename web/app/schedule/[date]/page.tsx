import { notFound } from 'next/navigation';
import { GameCardCompact } from '@/components/prediction/GameCardCompact';
import { EmptyState } from '@/components/ui/EmptyState';
import { SAMPLE_GAMES } from '@/lib/fixtures/today';

export const dynamic = 'force-static';

// 静的生成は直近5シーズンの範囲内の日付のみ。範囲外は 404 で即座に打ち切る
// （URL 空間が無限だとクローラの総当たりで無料枠が枯渇する。要件 8.2）。
// 11a は合成データの2日分だけを生成する。
const SAMPLE_DATES = ['2026-09-22', '2026-09-23'] as const;

export function generateStaticParams() {
  return SAMPLE_DATES.map((date) => ({ date }));
}

export default async function Page({ params }: { params: Promise<{ date: string }> }) {
  const { date } = await params;
  if (!SAMPLE_DATES.includes(date as (typeof SAMPLE_DATES)[number])) notFound();

  const games = date === '2026-09-22' ? SAMPLE_GAMES : [];
  const [, month, day] = date.split('-');

  return (
    <>
      <h2 className="mt-5 text-[19px] font-extrabold">
        {Number(month)}月{Number(day)}日の試合
      </h2>
      <div className="mt-3 flex flex-col gap-3">
        {games.length > 0 ? (
          games.map((game) => <GameCardCompact key={game.gameId} game={game} />)
        ) : (
          <EmptyState
            message={`${Number(month)}月${Number(day)}日に予定されている試合はありません。`}
            action={{ href: '/schedule/2026-09-22/', label: '次の試合は 9月22日（火）19:05 です' }}
          />
        )}
      </div>
    </>
  );
}
