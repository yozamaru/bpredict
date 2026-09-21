/**
 * 更新遅延はヘッダー直下の全幅バナー（要件 8.4）。
 * 判定は「`status = 'SUCCESS'` の最新から24時間以上経過」（基本設計 4.5）。
 */
export function StaleBanner({ generatedAtLabel }: { generatedAtLabel: string }) {
  return (
    <p className="mt-3 rounded-xl bg-warn-bg px-3 py-2 text-[12px] leading-relaxed text-warn">
      表示中の予測は{generatedAtLabel}時点のものです。その後の欠場情報は反映されていません。
    </p>
  );
}
