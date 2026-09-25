import Link from 'next/link';
import { ResultComparison } from '@/components/prediction/ResultComparison';
import { EmptyState } from '@/components/ui/EmptyState';
import { SAMPLE_RESULTS } from '@/lib/fixtures/results';
import { ACTIONS, OFF_SEASON } from '@/lib/messages';

export const dynamic = 'force-static';
export const metadata = { title: '結果 | B.PREDICT（仮称）' };

export default function Page() {
  return (
    <>
      <h2 className="mt-5 text-[19px] font-extrabold">結果</h2>
      <p className="mt-3 rounded-xl border border-border px-3 py-2 text-[11px] leading-relaxed text-text-2">
        これは表示を確認するための合成データです。実際の結果ではありません。
      </p>
      {/* 外れた試合を隠さない（要件 8.3）。並びも扱いも的中と同じにする */}
      <p className="mt-2 text-[11px] leading-relaxed text-text-3">
        予測を外した試合も同じ並びで出しています。それぞれに、その確率帯の通算成績を添えました。
      </p>

      <div className="mt-3 flex flex-col gap-3">
        {SAMPLE_RESULTS.length > 0 ? (
          SAMPLE_RESULTS.map((result) => (
            <ResultComparison key={result.gameId} result={result} />
          ))
        ) : (
          // オフシーズンは**的中率ページへ送る**（要件 8.5）。
          // 年間の1/3以上がオフシーズンであり、空状態は主要画面の一つである
          <EmptyState message={OFF_SEASON} action={ACTIONS.accuracy} />
        )}
      </div>

      <Link
        href={ACTIONS.accuracy.href}
        className="mt-4 flex min-h-11 items-center justify-center rounded-xl border border-border text-[13px] font-bold"
      >
        通算の的中率と較正を見る
      </Link>
    </>
  );
}
