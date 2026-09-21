import Link from 'next/link';
import { ThemeToggle } from '@/components/ui/ThemeToggle';

// タブは nav + a で実装し、最低高さ44px、現在地に aria-current を付ける（詳細設計 5.2）
const TABS = [
  { href: '/', label: '今日の予測' },
  { href: '/results/', label: '結果' },
  { href: '/accuracy/', label: '的中率' },
] as const;

export function Header({ current = '/' }: { current?: string }) {
  return (
    <header className="pt-4">
      <div className="flex items-center justify-between">
        <Link href="/" className="flex min-h-11 items-center text-[19px] font-extrabold">
          B.PREDICT
          <span className="ml-1.5 text-[10px] font-bold tracking-wider text-text-2">仮称</span>
        </Link>
        <ThemeToggle />
      </div>
      <nav aria-label="主要な画面" className="-mx-1 mt-1 flex">
        {TABS.map((tab) => {
          const active = tab.href === current;
          return (
            <Link
              key={tab.href}
              href={tab.href}
              aria-current={active ? 'page' : undefined}
              className={`flex min-h-11 items-center px-3 text-[13px] font-bold ${
                active
                  ? 'border-b-2 border-accent text-text'
                  : 'border-b-2 border-transparent text-text-2'
              }`}
            >
              {tab.label}
            </Link>
          );
        })}
      </nav>
    </header>
  );
}
