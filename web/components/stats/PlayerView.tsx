'use client';

// 選手別（要件 F-15 / 詳細設計 3.3 の `/players/:playerId` / 基本設計 5.2）。
//
// **出すのは集計値だけである**（要件 3.1.1）。試合ごとの記録を並べない。
// **予測値を1つも出さない** — この画面に出るのはすべて実績である。
//
// 2026-10-08 にスコアボード型へ移した（基本設計 6.1〜6.3）。**通算の主数値が
// 26px になり**（`StatSummaryTable` の `total`）、**季のクラブ名は行の2行目へ
// 下げた**（`sublabel`。横幅を数値に譲る）。

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { StatSummaryTable, type StatRow } from '@/components/stats/StatSummaryTable';
import { ACTUAL_NOT_RATING, CAREER_RANGE, StatNotes } from '@/components/stats/StatNotes';
import { EmptyState } from '@/components/ui/EmptyState';
import { ACTIONS, LOADING, LOAD_ERROR, NO_STATS_YET } from '@/lib/messages';
import { fetchPlayer, type Player } from '@/lib/source';

/** 主要項目の見出し。`cells` と同じ数・同じ順（詳細設計 5.3） */
const COLUMNS = ['分', '得点', 'R', 'A'] as const;

export function PlayerView({ playerId }: { playerId: string }) {
  const [player, setPlayer] = useState<Player | null>(null);
  const [empty, setEmpty] = useState(false);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    fetchPlayer(playerId)
      .then((found) => {
        if (alive) setPlayer(found);
      })
      .catch((error: unknown) => {
        if (!alive) return;
        // **集計がまだない状態を「エラー」と出さない**（要件 8.5）。
        // 静的生成の範囲内でも、集計が走る前は 404 になる（詳細設計 3.3）
        if (error instanceof Error && error.message.includes('404')) setEmpty(true);
        else setFailed(true);
      });
    return () => {
      alive = false;
    };
  }, [playerId]);

  if (failed) return <EmptyState message={LOAD_ERROR} action={ACTIONS.about} />;
  if (empty) return <EmptyState message={NO_STATS_YET} action={ACTIONS.today} />;
  if (player === null) {
    return (
      <p
        aria-live="polite"
        className="mt-5 rounded-xs bg-panel px-3 py-4 text-center text-[15px] text-ink-3"
      >
        {LOADING}
      </p>
    );
  }

  const rows: StatRow[] = [];
  // **通算を先に出す**（基本設計 5.2）。まず全体の水準を示す
  if (player.career !== null) {
    rows.push({
      key: 'career',
      label: '通算',
      // **主数値を 26px のカードで出す**（基本設計 6.2）
      total: true,
      games: player.career.games,
      cells: [
        player.career.perGame.minutes,
        player.career.perGame.pts,
        player.career.perGame.reb,
        player.career.perGame.ast,
      ],
      box: player.career.box,
    });
  }
  for (const season of player.seasons) {
    const club = season.club.shortName ?? season.club.name;
    rows.push({
      // **季中に移籍した季は2行になる**（クラブ別に持つため。詳細設計 1.9）。
      // 鍵に季とクラブの両方を含める
      key: `${season.seasonId}/${season.club.slug ?? ''}`,
      label: season.label ?? season.seasonId,
      // **クラブ名は2行目に置く**（横幅を数値に譲る。基本設計 6.3）
      sublabel: club ?? undefined,
      games: season.games,
      cells: [
        season.perGame.minutes,
        season.perGame.pts,
        season.perGame.reb,
        season.perGame.ast,
      ],
      box: season.box,
    });
  }

  return (
    <>
      {/* **画面の見出しだけが `--ink` を使う**（主数値と共有。基本設計 6.1） */}
      <h2 className="mt-5 font-serif text-[21px] font-semibold tracking-[0.02em] text-ink">
        {player.player.name}
      </h2>

      {player.current !== null && (
        <p className="mt-1.5 text-[15px] text-ink-3">
          <Link href={`/teams/${player.current.clubSlug}/`} className="underline">
            {player.current.clubName ?? player.current.clubSlug}
          </Link>
          {player.current.position !== null && <span> ・ {player.current.position}</span>}
          {player.current.number !== null && <span className="num"> #{player.current.number}</span>}
        </p>
      )}

      {rows.length === 0 ? (
        <div className="mt-4">
          <EmptyState message={NO_STATS_YET} action={ACTIONS.today} />
        </div>
      ) : (
        <StatSummaryTable heading="成績" columns={COLUMNS} rows={rows} />
      )}

      <StatNotes notes={[CAREER_RANGE, ACTUAL_NOT_RATING]} />
    </>
  );
}
