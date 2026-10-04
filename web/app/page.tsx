import { TodayView } from '@/components/prediction/TodayView';

// 静的出力。ISR は使わない（要件 7章）
export const dynamic = 'force-static';

/**
 * 今日の予測（基本設計 5.2）。
 *
 * **データの取得は `TodayView`（クライアント）が行う**（基本設計 5.6）。
 * ビルド時に埋め込むと、バッチが1日4回書き換えるたびに再ビルドが要る。
 * ルート自体は静的出力のままで、クライアントの JS をあの木だけに閉じている。
 *
 * **凡例はここに置く。** 取得の成否に関わらず出る説明であり、
 * 取得待ちの間も読める（しきい値を画面に描くのが要件である。詳細設計 5.3）。
 */
export default function Page() {
  return (
    <>
      <TodayView />

      <p className="mt-3 border border-dashed border-rule px-[7px] py-[5px] text-[10px] leading-relaxed text-ink-3">
        <b className="font-bold text-ink-2">H</b>＝ホーム／
        <b className="font-bold text-ink-2">A</b>＝アウェイ。バーは勝率の
        <b className="font-bold text-ink-2">50%からの隔たり</b>
        を優勢な側へ伸ばしたもの。
        <span
          aria-hidden="true"
          className="mx-0.5 inline-block h-[9px] w-4 border-x border-axis bg-band align-[-1px]"
        />
        の網かけ帯（±5ポイント）に収まっていれば
        <b className="font-bold text-ink-2">ほぼ互角</b>で、
        バーが短い試合ほどモデルが読めていないことを示します。
      </p>
    </>
  );
}
