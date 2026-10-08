import { ResultsView } from '@/components/prediction/ResultsView';

export const dynamic = 'force-static';
export const metadata = { title: '結果 | B.PREDICT' };

/**
 * 結果（要件 F-09）。
 *
 * **取得はクライアントで行う**（基本設計 5.6）。見る日は `meta.json` の
 * `latestResultDate` で決め、**時計を見ない**（詳細設計 3.7 / 5.6）。
 */
export default function Page() {
  return <ResultsView />;
}
