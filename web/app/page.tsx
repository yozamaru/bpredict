import { GameCard } from '@/components/prediction/GameCard';
import { GameCardCompact } from '@/components/prediction/GameCardCompact';
import { StaleBanner } from '@/components/prediction/StaleBanner';
import {
  SAMPLE_ACCURACY,
  SAMPLE_DATE_LABEL,
  SAMPLE_GAMES,
  SAMPLE_STALE,
} from '@/lib/fixtures/today';

// 静的出力。ISR は使わない（要件 7章）
export const dynamic = 'force-static';

export default function Page() {
  const [featured, ...rest] = SAMPLE_GAMES;

  return (
    <>
      {SAMPLE_STALE.stale && <StaleBanner generatedAtLabel={SAMPLE_STALE.generatedAtLabel} />}

      {/* 工程11a の間だけ出す。実データへ結線する 11b で外す */}
      <p className="mt-3 rounded-xl border border-border px-3 py-2 text-[11px] leading-relaxed text-text-2">
        これは表示を確認するための合成データです。実際の予測ではなく、クラブ名も架空です。
      </p>

      <h2 className="mt-5 text-[19px] font-extrabold">{SAMPLE_DATE_LABEL}の試合</h2>

      <div className="mt-3 flex flex-col gap-3">
        {featured && <GameCard game={featured} accuracy={SAMPLE_ACCURACY} />}
        {rest.map((game) => (
          <GameCardCompact key={game.gameId} game={game} />
        ))}
      </div>
    </>
  );
}
