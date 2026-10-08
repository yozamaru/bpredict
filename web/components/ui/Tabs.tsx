'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';

// タブは nav + a で実装し、最低高さ44px、現在地に aria-current を付ける（詳細設計 5.2）
// **`scripts/check-nav.mjs` がこの配列を読む。** 書き写さない（タブを足したときに
// 検査が追随しないため。詳細設計 5.6）
const TABS = [
  { href: '/', label: '今日の予測' },
  { href: '/results/', label: '結果' },
  { href: '/accuracy/', label: '的中率' },
  // **これが無いと、チーム別と選手別へたどり着けない**（要件 8.2 / 基本設計 5.1）。
  // 試合詳細からだけでは、試合が無い日に30クラブと557人のどれにも行けない
  { href: '/teams/', label: 'チーム' },
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
 *
 * **現在地は面の差で示す**（基本設計 6.3。スコアボード型では線は意味を持つものだけ）。
 * 旧版は `--ink` の下線を引いていたが、**`--ink` は主数値と画面見出しに予約する**
 * という 6.1 の規約と食い違っていた。
 * **色だけで伝えない** — `aria-current` と文字の濃さも併せて変える（要件 8.6）。
 */
export function Tabs() {
  const current = normalize(usePathname() || '/');
  return (
    <nav aria-label="主要な画面" className="mt-2 flex gap-1">
      {TABS.map((tab) => {
        const active = normalize(tab.href) === current;
        return (
          <Link
            key={tab.href}
            href={tab.href}
            aria-current={active ? 'page' : undefined}
            className={`flex min-h-11 items-center rounded-xs px-3 text-[14px] font-bold ${
              active ? 'bg-strip text-ink-2' : 'text-ink-3'
            }`}
          >
            {tab.label}
          </Link>
        );
      })}
    </nav>
  );
}
