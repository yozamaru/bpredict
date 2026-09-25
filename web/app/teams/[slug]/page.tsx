import { notFound } from 'next/navigation';
import { HistoryRow } from '@/components/prediction/HistoryRow';
import { EmptyState } from '@/components/ui/EmptyState';
import { SAMPLE_HISTORY, SAMPLE_TEAM } from '@/lib/fixtures/team';
import { ACTIONS, OFF_SEASON } from '@/lib/messages';

export const dynamic = 'force-static';

// クラブの slug は運営者が決めた恒久の識別子で、**改称があっても変えない**
// （詳細設計 1.1）。表示名は `club_seasons.name` が持つ。
// 11a は合成データの1件だけ。実クラブの slug への差し替えは 11b。
const SAMPLE_SLUGS = ['demo-alphas'] as const;

export function generateStaticParams() {
  return SAMPLE_SLUGS.map((slug) => ({ slug }));
}

export default async function Page({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  if (!SAMPLE_SLUGS.includes(slug as (typeof SAMPLE_SLUGS)[number])) notFound();
  const team = SAMPLE_TEAM;

  return (
    <>
      <h2 className="mt-5 text-[19px] font-extrabold">{team.name}</h2>
      <p className="mt-3 rounded-xl border border-border px-3 py-2 text-[11px] leading-relaxed text-text-2">
        これは表示を確認するための合成データです。クラブ名も架空です。
      </p>

      <section className="mt-4">
        <h3 className="sr-only">今季の成績</h3>
        <dl className="grid grid-cols-2 gap-2">
          <div className="rounded-2xl border border-border bg-surface p-3">
            <dt className="text-[11px] font-bold tracking-wider text-text-2">今季</dt>
            <dd className="mt-0.5 text-[24px] font-extrabold tabular-nums">
              {team.record.wins}
              <span className="text-[14px] text-text-2">勝</span> {team.record.losses}
              <span className="text-[14px] text-text-2">敗</span>
            </dd>
          </div>
          <div className="rounded-2xl border border-border bg-surface p-3">
            <dt className="text-[11px] font-bold tracking-wider text-text-2">平均得失点差</dt>
            <dd className="mt-0.5 text-[24px] font-extrabold tabular-nums">
              {team.avgMargin > 0 ? '＋' : ''}
              {team.avgMargin.toFixed(1)}
            </dd>
          </div>
          <div className="rounded-2xl border border-border bg-surface p-3">
            <dt className="text-[11px] font-bold tracking-wider text-text-2">直近5試合</dt>
            <dd className="mt-0.5 text-[16px] font-extrabold tracking-wider">
              {team.last5.join(' ')}
            </dd>
          </div>
          <div className="rounded-2xl border border-border bg-surface p-3">
            {/* 的中率には母数を併記する（要件 8.3） */}
            <dt className="text-[11px] font-bold tracking-wider text-text-2">このクラブの的中率</dt>
            <dd className="mt-0.5 text-[16px] font-extrabold tabular-nums">
              {(team.accuracy.rate * 100).toFixed(1)}%
              <span className="text-[12px] font-bold text-text-2">（{team.accuracy.n}試合）</span>
            </dd>
          </div>
        </dl>
        {/* Elo は専門用語であり、一般向けの言い換えを先に置く（要件 8.3） */}
        <p className="mt-2 text-[11px] leading-relaxed text-text-3">
          対戦結果から算出した相対的な強さの指標は {team.elo}（リーグ平均 1500）。
          数字が大きいほど強いと見ています。
        </p>
      </section>

      <section className="mt-6">
        <h3 className="text-[15px] font-extrabold">予測の履歴</h3>
        {/* 外れた試合を隠さない（要件 8.3）。良い時も悪い時も同じ場所に出す */}
        <p className="mt-1 text-[11px] leading-relaxed text-text-3">
          的中した試合も外した試合も、同じ並びで出しています。
        </p>
        {SAMPLE_HISTORY.length > 0 ? (
          <ul className="mt-2 rounded-2xl border border-border bg-surface px-3">
            {SAMPLE_HISTORY.map((item) => (
              <HistoryRow key={item.gameId} item={item} />
            ))}
          </ul>
        ) : (
          <div className="mt-2">
            <EmptyState message={OFF_SEASON} action={ACTIONS.accuracy} />
          </div>
        )}
      </section>
    </>
  );
}
