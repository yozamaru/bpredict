'use client';

import { useSyncExternalStore } from 'react';

type Theme = 'light' | 'dark';

/**
 * 既定はライト。OS 設定への追従は行わない（詳細設計 5.4）。
 *
 * 現在のテーマは DOM（layout の inline script が設定済み）という外部の値なので、
 * `useSyncExternalStore` で読む。`useEffect` で setState する形にすると、
 * 「状態を導出するために効果を使う」ことになり React 19 の規則にも触れる。
 * サーバ側のスナップショットはライト固定で、ハイドレーション後に実際の値へ寄る。
 */
const listeners = new Set<() => void>();

function subscribe(onChange: () => void): () => void {
  listeners.add(onChange);
  return () => {
    listeners.delete(onChange);
  };
}

function clientSnapshot(): Theme {
  return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
}

function serverSnapshot(): Theme {
  return 'light';
}

function apply(next: Theme): void {
  document.documentElement.setAttribute('data-theme', next);
  try {
    localStorage.setItem('theme', next);
  } catch {
    // 保存できない環境でも表示の切替は成立させる（CLAUDE.md UI）
  }
  listeners.forEach((listener) => listener());
}

export function ThemeToggle() {
  const theme = useSyncExternalStore(subscribe, clientSnapshot, serverSnapshot);

  return (
    <button
      type="button"
      onClick={() => apply(theme === 'dark' ? 'light' : 'dark')}
      aria-pressed={theme === 'dark'}
      // **線で囲まない**（基本設計 6.3。線は意味を持つものだけ）。面の差で押せることを示す
      className="flex min-h-11 min-w-11 items-center justify-center rounded-xs bg-panel px-3 text-[12px] font-bold text-ink-2"
    >
      {theme === 'dark' ? 'ダーク' : 'ライト'}
    </button>
  );
}
