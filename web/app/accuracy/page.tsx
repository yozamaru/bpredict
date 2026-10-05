import { AccuracyView } from '@/components/prediction/AccuracyView';

export const dynamic = 'force-static';
export const metadata = { title: '的中率 | B.PREDICT（仮称）' };

/**
 * 的中率（要件 F-07）。**`accuracy_summary` から表示する**（工程14）。
 *
 * **取得はクライアントで行う**（基本設計 5.6）。ビルド時に埋め込むと、結果照合が
 * 走るたびに再ビルドが要る。
 */
export default function Page() {
  return (
    <>
      <h2 className="mt-5 font-serif text-[19px] font-semibold tracking-[0.02em]">的中率</h2>
      <AccuracyView />
    </>
  );
}
