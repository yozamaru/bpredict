'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';

// タブは nav + a で実装し、最低高さ44px、現在地に aria-current を付ける（詳細設計 5.2）
const TABS = [
  { href: '/', label: '今日の予測' },
  { href: '/results/', label: '結果' },
  { href: '/accuracy/', label: '的中率' },
] as const;

/**
 * 末尾スラッシュを揃える。`next.config.ts` が `trailingSlash: true` なので配信される
 * パスは `/results/` だが、**判定をどちらか一方の形に頼らない**。
 */
function normalize(path: string): string {
  return path.endsWith('/') ? path : `${path}/`;
}

/**
 * **現在地はパスから導く。** 呼び出し側から渡すと、渡し忘れたページが静かに
 * 「今日の予測」を現在地として描く（実際にそうなっていた。`layout.tsx` が
 * 引数なしで描いており、下線も `aria-current` も動かなかった）。
 */
export function Tabs() {
  const current = normalize(usePathname() || '/');
  return (
    <nav aria-label="主要な画面" className="-mx-1 mt-1 flex">
      {TABS.map((tab) => {
        const active = normalize(tab.href) === current;
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
  );
}
