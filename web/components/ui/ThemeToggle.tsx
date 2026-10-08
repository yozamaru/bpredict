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

/**
 * 太陽と月。**インライン SVG で描く**（要件 8.1。絵文字もアイコンフォントも使わない）。
 *
 * **出すのは「いまの状態」ではなく「押したら切り替わる先」である**（詳細設計 5.3。
 * 運営者の指摘。2026-10-08）。**いまの状態は画面全体が既に伝えている** — 地が
 * 明るいか暗いかで分かるため、18px のアイコンで繰り返しても情報が増えない。
 * 一方、押したら何が起きるかは**このアイコンしか伝える場所がない**。
 *
 * `aria-pressed` との向きも揃う — ライトでは `false`（ダークはオフ）＋月、
 * ダークでは `true`＋太陽で、どちらも「ダークであるか」を同じ向きで指す。
 *
 * **`aria-hidden` を付ける。** 名前はボタンの `aria-label` が持つ。両方に読み上げ
 * 対象があると二重になる（詳細設計 5.2 の確率ブロックと同じ理由）。
 *
 * **色だけで状態を伝えない**（要件 8.6）。太陽と月は**形が違う**ので、
 * 単色のままでも区別できる。線で描き、塗りは使わない。
 */
function Icon({ theme }: { theme: Theme }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      width="18"
      height="18"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      {theme === 'dark' ? (
        // ダークのとき**太陽**（押すとライトへ切り替わる）。円と8本の光条
        <>
          <circle cx="12" cy="12" r="4.2" />
          <path d="M12 2.5v2.2M12 19.3v2.2M2.5 12h2.2M19.3 12h2.2M5.2 5.2l1.6 1.6M17.2 17.2l1.6 1.6M18.8 5.2l-1.6 1.6M6.8 17.2l-1.6 1.6" />
        </>
      ) : (
        // ライトのとき**月**（押すとダークへ切り替わる）。欠けた円で描く
        <path d="M20.5 14.3A8.5 8.5 0 0 1 9.7 3.5a8.5 8.5 0 1 0 10.8 10.8Z" />
      )}
    </svg>
  );
}

export function ThemeToggle() {
  const theme = useSyncExternalStore(subscribe, clientSnapshot, serverSnapshot);
  const dark = theme === 'dark';

  return (
    <button
      type="button"
      onClick={() => apply(dark ? 'light' : 'dark')}
      aria-pressed={dark}
      /*
       * **名前を文字で持つ**（要件 8.6。アイコンだけのボタンには `aria-label` を与える）。
       *
       * **押すと何が起きるかではなく、何の切替かを名前にする。** `aria-pressed` が
       * 状態を持つため、名前を「ダークにする」にすると状態と動作が二重になり、
       * 押した直後に名前が変わって読み上げが追えない。
       */
      aria-label="ダークモード"
      // **線で囲まない**（基本設計 6.3。線は意味を持つものだけ）。面の差で押せることを示す
      className="flex min-h-11 min-w-11 items-center justify-center rounded-xs bg-panel text-ink-2"
    >
      <Icon theme={theme} />
    </button>
  );
}
