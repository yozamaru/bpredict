import Link from 'next/link';
import { notFound } from 'next/navigation';
import { ScheduleView } from '@/components/prediction/ScheduleView';
import { dateLabel, shiftDate } from '@/lib/map';
import { staticDates } from '@/lib/seasons';

export const dynamic = 'force-static';

/**
 * **静的生成は直近3シーズンの範囲内の日付のみ**（要件 8.2）。
 *
 * 範囲外は 404 で即座に打ち切る — URL 空間が無限だとクローラの総当たりで
 * 無料枠が枯渇する。**範囲は `seasons.csv` から読む**（詳細設計 1.1）。
 */
export function generateStaticParams() {
  return staticDates().map((date) => ({ date }));
}

export default async function Page({ params }: { params: Promise<{ date: string }> }) {
  // Next.js 16 では Promise である（同期アクセスは型エラー）
  const { date } = await params;
  const dates = staticDates();
  if (!dates.includes(date)) notFound();

  // **生成した範囲の外へリンクしない。** 範囲外は 404 であり、導線から 404 へ送らない
  const previous = dates.includes(shiftDate(date, -1)) ? shiftDate(date, -1) : null;
  const next = dates.includes(shiftDate(date, 1)) ? shiftDate(date, 1) : null;

  return (
    <>
      <h2 className="mt-5 font-serif text-[19px] font-semibold tracking-[0.02em]">
        {dateLabel(date)}の試合
      </h2>

      {/* 前後の日付。タップ領域は最低 44px（要件 8.6） */}
      <nav aria-label="日付の移動" className="mt-2.5 flex items-stretch gap-2">
        {previous !== null && (
          <Link
            href={`/schedule/${previous}/`}
            className="flex min-h-11 flex-1 items-center justify-center border border-rule text-[13px] font-bold"
          >
            ← {dateLabel(previous)}
          </Link>
        )}
        {next !== null && (
          <Link
            href={`/schedule/${next}/`}
            className="flex min-h-11 flex-1 items-center justify-center border border-rule text-[13px] font-bold"
          >
            {dateLabel(next)} →
          </Link>
        )}
      </nav>

      <div className="mt-3">
        <ScheduleView date={date} />
      </div>
    </>
  );
}
