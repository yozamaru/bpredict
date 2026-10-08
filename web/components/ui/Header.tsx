import Link from 'next/link';
import { Tabs } from '@/components/ui/Tabs';
import { ThemeToggle } from '@/components/ui/ThemeToggle';

/**
 * 外枠の頭。**ブランドを画面の主役にしない**（基本設計 6.1 / 6.2。スコアボード型）。
 *
 * `--ink` は**主数値と画面見出し（h2）だけに予約する**ため、ブランドは `--ink-2`。
 * いちばん大きい字は日付の見出しと勝率の数値であって、サイト名ではない。
 */
export function Header() {
  return (
    <header className="pt-2">
      <div className="flex items-center justify-between">
        <Link
          href="/"
          className="flex min-h-11 items-center gap-2 text-[15px] font-bold tracking-[0.18em] text-ink-2"
        >
          B.PREDICT
          {/* 名称は未決（未決事項 U-01）。**確定するまで隠さない** */}
          <span className="rounded-xs bg-strip px-1.5 py-0.5 text-[12px] font-bold tracking-normal text-ink-3">
            仮称
          </span>
        </Link>
        <ThemeToggle />
      </div>
      <Tabs />
    </header>
  );
}
