import Link from 'next/link';

/**
 * 空状態は例外処理ではなく主要画面の一つ（要件 8.5。年間の1/3以上がオフシーズン）。
 *
 * **文言は `lib/messages.ts` が持つ**（ここで作らない。要件 8.5）。
 * **線で囲まない**（基本設計 6.3。スコアボード型では線は意味を持つものだけで、
 * 地との差は面の明るさが作る）。
 */
export function EmptyState({
  message,
  action,
}: {
  message: string;
  action?: { href: string; label: string };
}) {
  return (
    <div className="rounded-xs bg-panel px-4 py-6 text-center">
      <p className="text-[15px] leading-relaxed text-ink-2">{message}</p>
      {action && (
        <Link
          href={action.href}
          className="mt-2 inline-flex min-h-11 items-center px-3 text-[14px] font-bold text-ink-2 underline underline-offset-4"
        >
          {action.label}
        </Link>
      )}
    </div>
  );
}
