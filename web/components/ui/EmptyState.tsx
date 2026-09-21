import Link from 'next/link';

/** 空状態は例外処理ではなく主要画面の一つ（要件 8.5。年間の1/3以上がオフシーズン） */
export function EmptyState({
  message,
  action,
}: {
  message: string;
  action?: { href: string; label: string };
}) {
  return (
    <div className="rounded-2xl border border-border bg-surface p-6 text-center">
      <p className="text-[14px] text-text-2">{message}</p>
      {action && (
        <Link
          href={action.href}
          className="mt-3 inline-flex min-h-11 items-center px-3 text-[13px] font-bold text-accent"
        >
          {action.label}
        </Link>
      )}
    </div>
  );
}
