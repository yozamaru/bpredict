'use client';

// クラブ別（要件 F-10 / 詳細設計 3.3 の `/teams/:slug`）。
//
// **公開API から読む**（基本設計 2.5 の表）。過去の成績と予測履歴は静的JSON の
// 窓（当日＋7日）に入らないため、ここは API が唯一の経路である。

import { useEffect, useState } from 'react';
import { HistoryRow, type HistoryView } from '@/components/prediction/HistoryRow';
import { RosterTable } from '@/components/stats/RosterTable';
import { CAREER_RANGE, FORFEIT_EXCLUDED, StatNotes } from '@/components/stats/StatNotes';
import { StatSummaryTable, type StatRow } from '@/components/stats/StatSummaryTable';
import { EmptyState } from '@/components/ui/EmptyState';
import { ACTIONS, LOADING, LOAD_ERROR } from '@/lib/messages';
import { dateLabel } from '@/lib/map';
import { fetchTeam, type Team, type TeamSeasonStat } from '@/lib/source';

/** 主要項目の見出し。`cells` と同じ数・同じ順（詳細設計 5.3） */
const COLUMNS = ['勝', '敗', '得点', '失点'] as const;

function teamRow(stat: TeamSeasonStat, key: string, label: string): StatRow {
  return {
    key,
    label,
    games: stat.games,
    cells: [stat.wins, stat.losses, stat.perGame.pointsFor, stat.perGame.pointsAgainst],
    box: stat.box,
    // **母数が2つある。** どちらが何の母数かを書く（詳細設計 5.3）
    denominatorLabel:
      `以下は1試合平均（ボックススコアの母数 ${stat.box.statGames}試合）`,
  };
}

export function TeamView({ slug, linkablePlayerIds }: {
  slug: string;
  /** 静的生成した選手ID。**範囲外にはリンクを張らない**（要件 8.2） */
  linkablePlayerIds: readonly string[];
}) {
  const [team, setTeam] = useState<Team | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    // **取得は `lib/source.ts` を通す。** 例外の扱いとキャッシュ指定を1か所にする
    fetchTeam(slug)
      .then((found) => {
        if (alive) setTeam(found);
      })
      .catch(() => {
        if (alive) setFailed(true);
      });
    return () => {
      alive = false;
    };
  }, [slug]);

  if (failed) return <EmptyState message={LOAD_ERROR} action={ACTIONS.about} />;
  if (team === null) {
    return (
      <p
        aria-live="polite"
        className="mt-5 border border-dashed border-rule px-2 py-3 text-center text-[12.5px] text-ink-3"
      >
        {LOADING}
      </p>
    );
  }

  const name = team.club.name ?? team.club.slug;
  // **実績が揃っている行だけを出す。** 中止・延期（VOID）は照合の母数から外れ、
  // スコアも判定も null になる（詳細設計 4.12）
  const history: HistoryView[] = team.history
    .filter(
      (item): item is typeof item & {
        ownScore: number;
        opponentScore: number;
        isCorrect: boolean;
      } =>
        item.ownScore !== null && item.opponentScore !== null && item.isCorrect !== null,
    )
    .map((item) => ({
      gameId: item.gameId,
      dateLabel: dateLabel(item.gameDate),
      isHome: item.isHome,
      opponentName: item.opponent.name ?? item.opponent.shortName ?? '—',
      ownWinProb: item.ownWinProb,
      ownScore: item.ownScore,
      opponentScore: item.opponentScore,
      isCorrect: item.isCorrect,
    }));

  // **通算を先に出す**（基本設計 5.2）。まず全体の水準を示す
  const statRows: StatRow[] = [];
  if (team.career !== null) statRows.push(teamRow(team.career, 'career', '通算'));
  for (const season of team.seasons) {
    statRows.push(
      teamRow(season, season.seasonId ?? 'unknown', season.label ?? season.seasonId ?? '—'),
    );
  }
  const linkable = new Set(linkablePlayerIds);

  return (
    <>
      <h2 className="mt-5 font-serif text-[19px] font-semibold tracking-[0.02em]">{name}</h2>

      <section className="mt-4">
        <h3 className="sr-only">今季の成績</h3>
        <dl className="grid grid-cols-2 border border-rule bg-panel">
          <div className="px-2 py-1.5">
            <dt className="text-[9.5px] tracking-[0.06em] text-ink-3">今季</dt>
            <dd className="mt-px font-mono text-[17px] font-semibold leading-tight text-ink">
              {team.record === null
                ? '—'
                : `${team.record.wins}勝 ${team.record.losses}敗`}
            </dd>
          </div>
          <div className="border-l border-rule-soft px-2 py-1.5">
            <dt className="text-[9.5px] tracking-[0.06em] text-ink-3">平均得失点差</dt>
            <dd className="mt-px font-mono text-[17px] font-semibold leading-tight text-ink">
              {team.avgMargin === null ? '—' : team.avgMargin.toFixed(1)}
            </dd>
          </div>
        </dl>
        {/* **Elo は専門用語である。** 言い換えを先に置く（要件 8.3） */}
        {team.elo !== null && (
          <p className="mt-1.5 text-[10.5px] leading-relaxed text-ink-3">
            過去の対戦結果から推定した
            <b className="font-bold text-ink-2">相対的な強さ</b>は
            <span className="font-mono"> {Math.round(team.elo)} </span>
            です（リーグ平均 1500）。
          </p>
        )}
      </section>

      <section className="mt-5">
        <h3 className="font-serif text-[16px] font-semibold">予測の履歴</h3>
        {team.accuracy !== null && team.accuracy.n > 0 && (
          // **母数を併記する**（要件 8.3）
          <p className="mt-1 font-mono text-[11.5px] text-ink-2">
            このクラブの試合の的中率 {(team.accuracy.accuracy * 100).toFixed(1)}%
            （{team.accuracy.n}試合）
          </p>
        )}
        {history.length > 0 ? (
          <ul className="mt-2 list-none border border-rule bg-panel p-0">
            {history.map((item) => (
              // **リンクを出さない。** 過去の試合には詳細ページが無い（`lib/routes.ts`）
              <HistoryRow key={item.gameId} item={item} linked={false} />
            ))}
          </ul>
        ) : (
          <div className="mt-2">
            <EmptyState message="照合済みの試合がまだありません。" action={ACTIONS.accuracy} />
          </div>
        )}
      </section>

      {statRows.length > 0 && (
        <StatSummaryTable heading="戦績" columns={COLUMNS} rows={statRows} />
      )}

      {team.roster.length > 0 && (
        <RosterTable entries={team.roster} canLink={(id) => linkable.has(id)} />
      )}

      {(statRows.length > 0 || team.roster.length > 0) && (
        <StatNotes notes={[CAREER_RANGE, FORFEIT_EXCLUDED]} />
      )}
    </>
  );
}
