import { GameCard } from '@/components/prediction/GameCard';
import { GameCardCompact } from '@/components/prediction/GameCardCompact';
import { StaleBanner } from '@/components/prediction/StaleBanner';
import {
  SAMPLE_ACCURACY,
  SAMPLE_DATE_LABEL,
  SAMPLE_GAMES,
  SAMPLE_STALE,
} from '@/lib/fixtures/today';
import { byTipoff } from '@/lib/view';
import { ACTIONS, noGames } from '@/lib/messages';
import { EmptyState } from '@/components/ui/EmptyState';

// 静的出力。ISR は使わない（要件 7章）
export const dynamic = 'force-static';

export default function Page() {
  // **開始時刻の昇順に並べる。** 一覧は日程であり、時刻順でないと読めない。
  // 並べ替えを画面側に持たせるのは、静的JSON の並びに依存しないようにするため
  const games = [...SAMPLE_GAMES].sort(byTipoff);
  // 一番早く始まる試合を大きく出す。**「次に始まる試合」にはしない** —
  // 静的配信では「いま」を知らず、時刻で変わる表示はキャッシュと噛み合わない
  const [featured, ...rest] = games;

  return (
    <>
      {SAMPLE_STALE.stale && <StaleBanner generatedAtLabel={SAMPLE_STALE.generatedAtLabel} />}

      {/* 工程11a の間だけ出す。実データへ結線する 11b で外す */}
      <p className="mt-3 rounded-xl border border-border px-3 py-2 text-[11px] leading-relaxed text-text-2">
        これは表示を確認するための合成データです。実際の予測ではなく、クラブ名も架空です。
      </p>

      <h2 className="mt-5 text-[19px] font-extrabold">{SAMPLE_DATE_LABEL}の試合</h2>

      {featured ? (
        <>
          <div className="mt-3 flex flex-col gap-3">
            <GameCard game={featured} accuracy={SAMPLE_ACCURACY} />
            {rest.map((game) => (
              <GameCardCompact key={game.gameId} game={game} />
            ))}
          </div>
          {/* 件数を出す。13試合の日は下まで長く、何試合あるのか分からないと読めない */}
          <p className="mt-3 text-[11px] text-text-3">全{games.length}試合</p>
        </>
      ) : (
        <div className="mt-3">
          <EmptyState message={noGames(SAMPLE_DATE_LABEL)} action={ACTIONS.accuracy} />
        </div>
      )}
    </>
  );
}
