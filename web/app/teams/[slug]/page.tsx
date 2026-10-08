import { notFound } from 'next/navigation';
import { TeamView } from '@/components/prediction/TeamView';
import { staticSlugs } from '@/lib/clubs';
import { staticPlayerIds } from '@/lib/players';
import { staticGameIds } from '@/lib/routes';

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
  // **静的生成した選手だけにリンクを張る**（要件 8.2。開けないリンクを置かない）。
  // ビルド時に読み、クライアントへ渡す
  return (
    <TeamView
      slug={slug}
      linkablePlayerIds={staticPlayerIds()}
      linkableGameIds={staticGameIds()}
    />
  );
}
