import { notFound } from 'next/navigation';
import { EmptyState } from '@/components/ui/EmptyState';

export const dynamic = 'force-static';

// クラブの slug は運営者が決めた恒久の識別子（詳細設計 1.1）。
// 11a は合成データの1件だけ。実クラブの slug への差し替えは 11b。
const SAMPLE_SLUGS = ['demo-alphas'] as const;

export function generateStaticParams() {
  return SAMPLE_SLUGS.map((slug) => ({ slug }));
}

export default async function Page({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  if (!SAMPLE_SLUGS.includes(slug as (typeof SAMPLE_SLUGS)[number])) notFound();

  return (
    <>
      <h2 className="mt-5 text-[19px] font-extrabold">架空アルファーズ</h2>
      <p className="mt-3 rounded-xl border border-border px-3 py-2 text-[11px] leading-relaxed text-text-2">
        クラブ別の予測履歴と成績は、実データへの結線（工程11b）で表示します。
      </p>
      <div className="mt-3">
        <EmptyState message="このクラブの予測履歴はまだありません。" />
      </div>
    </>
  );
}
