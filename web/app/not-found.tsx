import Link from 'next/link';
import { ACTIONS, NOT_FOUND } from '@/lib/messages';

/**
 * 404。**Next.js の既定を使わない**（`lib/messages.ts` の `NOT_FOUND` に理由）。
 *
 * 既定は英語で、しかも `prefers-color-scheme` を見る独自CSSを差し込むため、
 * 利用者が選んだテーマを無視して OS 設定に従ってしまう。
 *
 * **ここでデータを読まない。** 404 はクローラの総当たりが着地する場所であり
 * （シーズン範囲外の日付は D1 到達前に打ち切る。要件 4.2）、軽いままにする。
 */
export const metadata = { title: 'ページが見つかりません | B.PREDICT（仮称）' };

export default function NotFound() {
  return (
    <>
      <h2 className="mt-5 text-[19px] font-extrabold">ページが見つかりません</h2>
      <div className="mt-3 rounded-2xl border border-border bg-surface p-6 text-center">
        <p className="text-[14px] text-text-2">{NOT_FOUND}</p>
        <Link
          href="/"
          className="mt-3 inline-flex min-h-11 items-center px-3 text-[13px] font-bold text-accent"
        >
          今日の予測を見る
        </Link>
      </div>
      <p className="mt-3 text-[11px] leading-relaxed text-text-3">
        日付やクラブを指定して開いた場合、対象の範囲外だと このページになります。
      </p>
      <Link
        href={ACTIONS.about.href}
        className="mt-2 flex min-h-11 items-center justify-center rounded-xl border border-border text-[13px] font-bold"
      >
        {ACTIONS.about.label}
      </Link>
    </>
  );
}
