/**
 * 更新遅延はヘッダー直下の全幅バナー（要件 8.4）。
 * 判定は「`status = 'SUCCESS'` の最新から24時間以上経過」（基本設計 4.5）。
 *
 * **塗りチップと同じ地を使う**（2026-10-08。スコアボード型）。旧版は廃止した
 * 淡い地のトークンに注意色の文字を載せていたが、**ライトでは白地で 4.5:1 を
 * 満たす黄色がもう黄土色であり、アウェイの焦橙と 14° しか離れない**
 * （基本設計 6.1）。色相ではなく**塗りという形**で分ける。
 */
export function StaleBanner({ generatedAtLabel }: { generatedAtLabel: string }) {
  return (
    <p className="mt-3 rounded-[2px] bg-warn px-3.5 py-2.5 text-[15px] leading-relaxed text-warn-ink">
      表示中の予測は{generatedAtLabel}時点のものです。その後の欠場情報は反映されていません。
    </p>
  );
}
