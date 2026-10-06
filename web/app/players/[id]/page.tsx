import { notFound } from 'next/navigation';
import { PlayerView } from '@/components/stats/PlayerView';
import { staticPlayerIds } from '@/lib/players';

export const dynamic = 'force-static';

/**
 * 静的生成するのは**直近3シーズンに出場した選手**だけである（要件 8.2 / F-15）。
 *
 * 全期間の1,028人でも上限（18,000ファイル）には収まるが採らない — 8.2 が範囲を
 * 絞るのは「公開URLの空間に上限を設ける」ためでもあり、日付・試合・選手で別の
 * 原則を持つ理由がない。範囲外は `notFound()` とし、**リンクも出さない**。
 */
export function generateStaticParams() {
  return staticPlayerIds().map((id) => ({ id }));
}

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!staticPlayerIds().includes(id)) notFound();
  return <PlayerView playerId={id} />;
}
