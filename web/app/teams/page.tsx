import { TeamList } from '@/components/stats/TeamList';

export const dynamic = 'force-static';
export const metadata = { title: 'チーム | B.PREDICT（仮称）' };

/**
 * クラブ一覧（要件 8.2 / 基本設計 5.1）。**チーム別と選手別への導線である。**
 *
 * **これが無いと試合詳細からしか行けず、試合が無い日は30クラブと557人の
 * 選手ページのどれにも到達できなかった。** `GET /teams` は詳細設計 3.3 に
 * 「画面の導線用」と書かれており、**API の側は最初からこの画面のために
 * 作られていた**。
 *
 * **取得はクライアントで行う**（基本設計 5.6）。クラブの集合は季で変わるため、
 * ビルド時に埋め込むと季が替わるたびに再ビルドが要る。
 */
export default function Page() {
  return (
    <>
      <h2 className="mt-5 font-serif text-[19px] font-semibold tracking-[0.02em]">チーム</h2>
      <TeamList />
    </>
  );
}
