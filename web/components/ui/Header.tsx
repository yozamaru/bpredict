import Link from 'next/link';
import { Tabs } from '@/components/ui/Tabs';
import { ThemeToggle } from '@/components/ui/ThemeToggle';

export function Header() {
  return (
    <header className="pt-4">
      <div className="flex items-center justify-between">
        <Link href="/" className="flex min-h-11 items-center text-[19px] font-extrabold">
          B.PREDICT
          <span className="ml-1.5 text-[10px] font-bold tracking-wider text-text-2">仮称</span>
        </Link>
        <ThemeToggle />
      </div>
      <Tabs />
    </header>
  );
}
