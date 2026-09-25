import Link from 'next/link';
import { notFound } from 'next/navigation';
import { GameCardCompact } from '@/components/prediction/GameCardCompact';
import { EmptyState } from '@/components/ui/EmptyState';
import { SAMPLE_FULL_DAY, SAMPLE_GAMES } from '@/lib/fixtures/today';
import { noGames } from '@/lib/messages';
import { byTipoff } from '@/lib/view';

export const dynamic = 'force-static';

// 静的生成は直近3シーズンの範囲内の日付のみ。範囲外は 404 で即座に打ち切る
// （URL 空間が無限だとクローラの総当たりで無料枠が枯渇する。要件 8.2）。
// 11a は合成データの3日分だけを生成する。
const SAMPLE_DATES = ['2026-09-22', '2026-09-23', '2026-09-24'] as const;
type SampleDate = (typeof SAMPLE_DATES)[number];

/** 試合がある日（合成データ）。ない日は空状態を出す */
const GAMES_BY_DATE: Record<SampleDate, typeof SAMPLE_GAMES> = {
  '2026-09-22': SAMPLE_GAMES,
  '2026-09-23': [],
  // **1日の最大構成（13試合）。** 圧縮カードだけの一覧がこの規模で読めるかを見る
  '2026-09-24': SAMPLE_FULL_DAY,
};

export function generateStaticParams() {
  return SAMPLE_DATES.map((date) => ({ date }));
}

const WEEKDAYS = ['日', '月', '火', '水', '木', '金', '土'] as const;

/** 「9月23日（水）」。**JST の暦日をそのまま読む**（`game_date` の定義。CLAUDE.md） */
function label(date: string): string {
  // `YYYY-MM-DD` は生成した範囲の値であり、形は保証されている。
  // それでも undefined を黙って通さない（曜日が1日ずれても誰も気づかない）
  const parts = date.split('-').map(Number);
  const [year, month, day] = parts;
  if (parts.length !== 3 || year === undefined || month === undefined || day === undefined) {
    throw new Error(`日付の形が違う: ${date}`);
  }
  const weekday = WEEKDAYS[new Date(Date.UTC(year, month - 1, day)).getUTCDay()];
  return `${month}月${day}日（${weekday}）`;
}

export default async function Page({ params }: { params: Promise<{ date: string }> }) {
  const { date } = await params;
  if (!SAMPLE_DATES.includes(date as SampleDate)) notFound();
  const current = date as SampleDate;
  const games = GAMES_BY_DATE[current];

  // **生成した範囲の外へリンクしない。** 範囲外は 404 であり、導線から 404 へ送らない
  const index = SAMPLE_DATES.indexOf(current);
  const previous = index > 0 ? SAMPLE_DATES[index - 1] : null;
  const next = index < SAMPLE_DATES.length - 1 ? SAMPLE_DATES[index + 1] : null;
  const nextWithGames = SAMPLE_DATES.slice(index + 1).find((d) => GAMES_BY_DATE[d].length > 0);

  return (
    <>
      <h2 className="mt-5 text-[19px] font-extrabold">{label(current)}の試合</h2>

      {/* 前後の日付。タップ領域は最低 44px（要件 8.6）。
          **範囲の端では何も描かない。** 無効の矢印を灰色で置くと「押せそうなのに
          押せない」見た目になり、しかも `--text-4`（罫線など非テキスト専用）を
          文字色に使うことになる（トークンの単体テストが検出する） */}
      <nav aria-label="日付の移動" className="mt-3 flex items-stretch gap-2">
        {previous && (
          <Link
            href={`/schedule/${previous}/`}
            className="flex min-h-11 flex-1 items-center justify-center rounded-xl border border-border text-[13px] font-bold"
          >
            ← {label(previous)}
          </Link>
        )}
        {next && (
          <Link
            href={`/schedule/${next}/`}
            className="flex min-h-11 flex-1 items-center justify-center rounded-xl border border-border text-[13px] font-bold"
          >
            {label(next)} →
          </Link>
        )}
      </nav>

      {games.length > 0 && (
        <p className="mt-3 text-[11px] text-text-3">全{games.length}試合</p>
      )}

      <div className="mt-2 flex flex-col gap-3">
        {games.length > 0 ? (
          [...games]
            .sort(byTipoff)
            .map((game) => <GameCardCompact key={game.gameId} game={game} />)
        ) : (
          <EmptyState
            message={noGames(label(current))}
            action={
              nextWithGames
                ? { href: `/schedule/${nextWithGames}/`, label: `次の試合日は ${label(nextWithGames)}` }
                : undefined
            }
          />
        )}
      </div>
    </>
  );
}
