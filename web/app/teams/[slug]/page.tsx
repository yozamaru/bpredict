import { notFound } from 'next/navigation';
import { TeamView } from '@/components/prediction/TeamView';
import { staticSlugs } from '@/lib/clubs';

export const dynamic = 'force-static';

/**
 * クラブの slug は運営者が決めた恒久の識別子で、**改称があっても変えない**
 * （詳細設計 1.1）。表示名は `club_seasons.name` が持つ。
 *
 * **出典は `db/seeds/master/clubs.csv`** — 全シーズンのクラブを生成する
 * （過去クラブのページも URL として生きている方がよい）。
 */
export function generateStaticParams() {
  return staticSlugs().map((slug) => ({ slug }));
}

export default async function Page({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  if (!staticSlugs().includes(slug)) notFound();
  return <TeamView slug={slug} />;
}
