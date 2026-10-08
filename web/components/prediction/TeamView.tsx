'use client';

// クラブ別（要件 F-10 / 詳細設計 3.3 の `/teams/:slug`）。
//
// **公開API から読む**（基本設計 2.5 の表）。過去の成績と予測履歴は静的JSON の
// 窓（当日＋7日）に入らないため、ここは API が唯一の経路である。
//
// 2026-10-08 にスコアボード型へ移した（基本設計 6.1〜6.3）。**今季の成績を
// 26px の2枚のボードにし**、**履歴と戦績の区切りを罫線から面の差に変えた**
// （`--rule-soft` は廃止された）。

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

function teamRow(stat: TeamSeasonStat, key: string, label: string, total = false): StatRow {
  return {
    key,
    label,
    total,
    games: stat.games,
    cells: [stat.wins, stat.losses, stat.perGame.pointsFor, stat.perGame.pointsAgainst],
    box: stat.box,
    // **母数が2つある。** どちらが何の母数かを書く（詳細設計 5.3）
    denominatorLabel:
      `以下は1試合平均（ボックススコアの母数 ${stat.box.statGames}試合）`,
  };
}

/** 今季の成績の1枚。**主数値は 26px**（基本設計 6.2） */
function Tile({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="rounded-xs bg-panel px-3 py-2.5">
      <dt className="text-[12px] font-bold tracking-[0.06em] text-ink-3">{label}</dt>
      <dd className="mt-1.5 flex items-baseline gap-0.5 text-[15px] text-ink-2">{children}</dd>
    </div>
  );
}

function Big({ children }: { children: React.ReactNode }) {
  return <span className="num text-[26px] leading-none text-ink">{children}</span>;
}

export function TeamView({ slug, linkablePlayerIds, linkableGameIds }: {
  slug: string;
  /** 静的生成した選手ID。**範囲外にはリンクを張らない**（要件 8.2） */
  linkablePlayerIds: readonly string[];
  /** 詳細ページがある試合ID（`public/data/games/index.json`。詳細設計 3.7） */
  linkableGameIds: readonly string[];
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
        className="mt-5 rounded-xs bg-panel px-3 py-4 text-center text-[15px] text-ink-3"
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
  if (team.career !== null) statRows.push(teamRow(team.career, 'career', '通算', true));
  for (const season of team.seasons) {
    statRows.push(
      teamRow(season, season.seasonId ?? 'unknown', season.label ?? season.seasonId ?? '—'),
    );
  }
  const linkable = new Set(linkablePlayerIds);
  const linkableGames = new Set(linkableGameIds);

  return (
    <>
      {/* **画面の見出しだけが `--ink` を使う**（主数値と共有。基本設計 6.1） */}
      <h2 className="mt-5 font-serif text-[21px] font-semibold tracking-[0.02em] text-ink">
        {name}
      </h2>

      <section className="mt-4">
        <h3 className="sr-only">今季の成績</h3>
        {/* **区切りは面の明るさの差で作る**（基本設計 6.3）。2px の地を覗かせる */}
        <dl className="grid grid-cols-2 gap-0.5">
          <Tile label="今季">
            {team.record === null ? (
              <Big>—</Big>
            ) : (
              <>
                <Big>{team.record.wins}</Big>
                <span>勝</span>
                <Big>{team.record.losses}</Big>
                <span>敗</span>
              </>
            )}
          </Tile>
          <Tile label="平均得失点差">
            <Big>{team.avgMargin === null ? '—' : team.avgMargin.toFixed(1)}</Big>
          </Tile>
        </dl>
        {/* **Elo は専門用語である。** 言い換えを先に置く（要件 8.3） */}
        {team.elo !== null && (
          <p className="mt-2 text-[15px] leading-relaxed text-ink-3">
            過去の対戦結果から推定した
            <b className="font-bold text-ink-2">相対的な強さ</b>は
            <span className="num"> {Math.round(team.elo)} </span>
            です（リーグ平均 <span className="num">1500</span>）。
          </p>
        )}
      </section>

      <section className="mt-6">
        <h3 className="font-serif text-[16px] font-semibold text-ink-2">予測の履歴</h3>
        {team.accuracy !== null && team.accuracy.n !== 0 && (
          // **母数を併記する**（要件 8.3）
          <p className="mt-1 text-[15px] text-ink-2">
            このクラブの試合の的中率{' '}
            <span className="num">{(team.accuracy.accuracy * 100).toFixed(1)}%</span>
            （<span className="num">{team.accuracy.n}</span>試合）
          </p>
        )}
        {history.length === 0 ? (
          <div className="mt-3">
            <EmptyState message="照合済みの試合がまだありません。" action={ACTIONS.accuracy} />
          </div>
        ) : (
          <ul className="mt-3 flex list-none flex-col gap-0.5 p-0">
            {history.map((item, at) => (
              // **索引にある試合だけリンクする**（`lib/routes.ts`）。予測を
              // 出した試合には詳細ページがあるが、予測を始める前（2026-10-05 より
              // 前）の試合には無い。**開けないリンクを置かない**
              <HistoryRow
                key={item.gameId}
                item={item}
                linked={linkableGames.has(item.gameId)}
                alt={at % 2 === 0}
              />
            ))}
          </ul>
        )}
      </section>

      {statRows.length !== 0 && (
        <StatSummaryTable heading="戦績" columns={COLUMNS} rows={statRows} />
      )}

      {team.roster.length !== 0 && (
        <RosterTable entries={team.roster} canLink={(id) => linkable.has(id)} />
      )}

      {(statRows.length !== 0 || team.roster.length !== 0) && (
        <StatNotes notes={[CAREER_RANGE, FORFEIT_EXCLUDED]} />
      )}
    </>
  );
}
