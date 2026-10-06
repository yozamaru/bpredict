// 当季の選手一覧（詳細設計 5.3）。
//
// **出典は `player_stat_summary`** であって `player_seasons` ではない（詳細設計 3.3）。
// あの断面は1試合も出ていない選手を含み、季中に離脱した選手が消えている。
//
// **ランキングの見た目にしない。** 順位の数字を振らない（要件 3.1.1）。並びは
// 読みやすさのためであって序列の主張ではない。

import Link from 'next/link';
import type { RosterEntry } from '@/lib/source';

function num(value: number | null, digits = 1): string {
  return value === null ? '—' : value.toFixed(digits);
}

export function RosterTable({
  entries,
  canLink,
}: {
  entries: readonly RosterEntry[];
  /** 静的生成の範囲内かどうか。**範囲外にはリンクを張らない**（要件 8.2） */
  canLink: (playerId: string) => boolean;
}) {
  return (
    <section className="mt-5">
      <h3 className="font-serif text-[16px] font-semibold">今季の選手</h3>
      <p className="mt-1 text-[10.5px] leading-relaxed text-ink-3">
        出場した選手を、出場時間の多い順に並べています。数値は1試合平均です。
      </p>
      <div className="mt-2 border border-rule bg-panel">
        <div
          aria-hidden="true"
          className="flex items-baseline gap-2 border-b border-rule px-2.5 py-1 text-[9.5px] tracking-[0.06em] text-ink-3"
        >
          <span className="flex-1">選手</span>
          <span className="w-12 text-right">試合</span>
          <span className="w-12 text-right">分</span>
          <span className="w-12 text-right">得点</span>
          <span className="w-10 text-right">R</span>
          <span className="w-10 text-right">A</span>
        </div>
        <ul className="list-none p-0">
          {entries.map((entry) => {
            const label = (
              <>
                <span className="flex-1 truncate text-[12.5px] font-bold">{entry.name}</span>
                {/* 未登録は「—」。**空にしない** — 列が詰まって見える */}
                <span className="w-10 shrink-0 text-[10.5px] text-ink-2">
                  {entry.position ?? '—'}
                  {entry.number !== null && (
                    <span className="font-mono"> #{entry.number}</span>
                  )}
                </span>
              </>
            );
            const cells = (
              <>
                <span className="w-12 text-right font-mono text-[12.5px]">{entry.games}</span>
                <span className="w-12 text-right font-mono text-[12.5px]">
                  {num(entry.perGame.minutes)}
                </span>
                <span className="w-12 text-right font-mono text-[12.5px] font-bold">
                  {num(entry.perGame.pts)}
                </span>
                <span className="w-10 text-right font-mono text-[12.5px]">
                  {num(entry.perGame.reb)}
                </span>
                <span className="w-10 text-right font-mono text-[12.5px]">
                  {num(entry.perGame.ast)}
                </span>
              </>
            );
            return (
              <li key={entry.playerId} className="border-b border-rule-soft last:border-b-0">
                {canLink(entry.playerId) ? (
                  <Link
                    href={`/players/${entry.playerId}/`}
                    className="flex min-h-11 items-baseline gap-2 px-2.5 py-2"
                  >
                    {label}
                    {cells}
                  </Link>
                ) : (
                  // **開けないリンクを置かない**（静的生成の範囲外。要件 8.2）
                  <div className="flex min-h-11 items-baseline gap-2 px-2.5 py-2">
                    {label}
                    {cells}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      </div>
    </section>
  );
}
