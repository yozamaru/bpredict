import { ResultComparison } from '@/components/prediction/ResultComparison';
import { SAMPLE_RESULTS } from '@/lib/fixtures/results';

export const dynamic = 'force-static';
export const metadata = { title: '結果 | B.PREDICT（仮称）' };

export default function Page() {
  return (
    <>
      <h2 className="mt-5 text-[19px] font-extrabold">結果</h2>
      <p className="mt-3 rounded-xl border border-border px-3 py-2 text-[11px] leading-relaxed text-text-2">
        これは表示を確認するための合成データです。実際の結果ではありません。
      </p>
      <div className="mt-3 flex flex-col gap-3">
        {SAMPLE_RESULTS.map((result) => (
          <ResultComparison key={result.gameId} result={result} />
        ))}
      </div>
    </>
  );
}
