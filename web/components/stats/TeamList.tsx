'use client';

// クラブ一覧（要件 8.2 / 基本設計 5.1・5.2 / 詳細設計 3.3 の `GET /teams`）。
//
// **これが無いと、チーム別と選手別へたどり着けない。** v1.60 までの導線は試合詳細
// からだけで、**試合が無い日は30クラブと557人の選手ページのどれにも到達できなかった**。
//
// **成績を出さない。** 一覧に並べると順位表に見え、要件 3.1.1 が「ランキングを
// 出さない」と定めているのに触れる。出すのは名前とリンクだけである。
//
// **並びは API が返す順をそのまま使う**（`clubs.slug` 昇順）。画面で並べ替えない —
// 並べ替えの規則を画面に持つと、API と食い違ったときにどちらが正かが決まらない。

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { EmptyState } from '@/components/ui/EmptyState';
import { ACTIONS, LOADING, LOAD_ERROR, NO_STATS_YET } from '@/lib/messages';
import { fetchTeams, type TeamList as TeamListData } from '@/lib/source';

export function TeamList() {
  const [data, setData] = useState<TeamListData | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    fetchTeams()
      .then((found) => {
        if (alive) setData(found);
      })
      .catch(() => {
        if (alive) setFailed(true);
      });
    return () => {
      alive = false;
    };
  }, []);

  if (failed) return <EmptyState message={LOAD_ERROR} action={ACTIONS.about} />;
  if (data === null) {
    return (
      <p
        aria-live="polite"
        className="mt-5 border border-dashed border-rule px-2 py-3 text-center text-[12.5px] text-ink-3"
      >
        {LOADING}
      </p>
    );
  }
  // **当季の `club_seasons` は最初の試合が終わるまで空になりうる**（詳細設計 4.2）。
  // そのとき「エラー」と出さない（要件 8.5）
  if (data.teams.length === 0) {
    return <EmptyState message={NO_STATS_YET} action={ACTIONS.today} />;
  }

  return (
    <section className="mt-5">
      <h2 className="font-serif text-[16px] font-semibold">クラブ</h2>
      <p className="mt-1 text-[11px] text-ink-3">
        クラブごとのシーズン別・通算の戦績と、当季の選手一覧を見られます。
      </p>
      <ul className="mt-2 border-t border-rule">
        {data.teams.map((team) => (
          <li key={team.clubId} className="border-b border-rule-soft">
            <Link
              href={`/teams/${team.slug}/`}
              className="flex min-h-11 items-center text-[13px] text-ink underline decoration-rule"
            >
              {team.name ?? team.shortName ?? team.slug}
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}
