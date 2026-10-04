import { notFound } from 'next/navigation';
import { GameDetailView } from '@/components/prediction/GameDetailView';
import { staticGameIds } from '@/lib/routes';

export const dynamic = 'force-static';

/**
 * **窓の中の試合（当日＋7日）だけを静的生成する。**
 *
 * 要件 8.2 は「直近3シーズン」の試合を生成すると定めるが、**試合IDの一覧を
 * ビルド時に得る経路が設計に無い**（`seasons.csv` は日付しか持たず、試合IDは
 * スナップショット = parquet にしかない）。コミット済みの静的JSON が唯一の
 * ビルド時に読める источник である（`lib/routes.ts`）。
 *
 * **試合IDは毎日増えるため、デプロイのたびに生成し直す必要がある。**
 * この2点は運営者の判断を待つ（`docs/STATUS.md`）。
 */
export function generateStaticParams() {
  return staticGameIds().map((id) => ({ id }));
}

// Next.js 16 では params が Promise（CLAUDE.md）
export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!staticGameIds().includes(id)) notFound();
  return <GameDetailView gameId={id} />;
}
