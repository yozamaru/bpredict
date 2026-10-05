'use client';

// 日付別（基本設計 2.5 / 詳細設計 3.7）。
//
// **窓の中は静的JSON、外は公開API**（基本設計 2.5 の表）。窓は当日＋7日で、
// それより前の日付は Workers API からクライアント fetch で表示する。

import { useEffect, useState } from 'react';
import { GameTable } from '@/components/prediction/GameTable';
import { EmptyState } from '@/components/ui/EmptyState';
import { ACTIONS, LOADING, LOAD_ERROR, noGames } from '@/lib/messages';
import { dateLabel, toGames } from '@/lib/map';
import { fetchGamesByDate, fetchSchedule } from '@/lib/source';
import { byTipoff, type GameView } from '@/lib/view';

/**
 * 窓の中かどうか（詳細設計 3.7）。**「いま」をクライアントで見る。**
 *
 * 静的ページは自分が何日に配信されたかを知らないため、窓の判定は端末の時計で
 * 行う。**外れても壊れない** — 静的JSON が無ければ API へ落ちるだけである。
 */
function inWindow(date: string, now: number = Date.now()): boolean {
  const today = new Date(now + 9 * 60 * 60 * 1000).toISOString().slice(0, 10);
  const target = Date.parse(`${date}T00:00:00Z`);
  const start = Date.parse(`${today}T00:00:00Z`);
  return target >= start && target <= start + 7 * 86_400_000;
}

export function ScheduleView({ date }: { date: string }) {
  const [games, setGames] = useState<GameView[] | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    const source = inWindow(date)
      ? fetchSchedule(date).catch(() => fetchGamesByDate(date))
      : fetchGamesByDate(date);
    source
      .then((list) => {
        if (alive) setGames(toGames(list));
      })
      .catch(() => {
        if (alive) setFailed(true);
      });
    return () => {
      alive = false;
    };
  }, [date]);

  if (failed) {
    return <EmptyState message={LOAD_ERROR} action={ACTIONS.about} />;
  }
  if (games === null) {
    return (
      <p
        aria-live="polite"
        className="border border-dashed border-rule px-2 py-3 text-center text-[12.5px] text-ink-3"
      >
        {LOADING}
      </p>
    );
  }
  if (games.length === 0) {
    return <EmptyState message={noGames(dateLabel(date))} action={ACTIONS.accuracy} />;
  }
  return <GameTable games={[...games].sort(byTipoff)} dateLabel={dateLabel(date)} />;
}
