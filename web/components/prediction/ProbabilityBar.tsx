import { isTossUp, percentPair, type GameView } from '@/lib/view';

/**
 * 勝率は**必ず両チーム分**を表示する（要件 8.3）。
 * ブロック全体に role="img" と一文の aria-label を与え、内部の数値とバーは
 * aria-hidden にする。両方に読み上げ対象があると重複する（詳細設計 5.2）。
 */
export function ProbabilityBar({ game, size = 'large' }: { game: GameView; size?: 'large' | 'compact' }) {
  const { home, away } = percentPair(game.homeWinProb);
  const label =
    `ホーム ${game.home.name} の勝率${home}パーセント、` +
    `アウェイ ${game.away.name} の勝率${away}パーセント。` +
    `予想スコア ${game.predHomeScore}対${game.predAwayScore}`;

  const number = size === 'large' ? 'text-4xl font-extrabold' : 'text-lg font-extrabold';

  return (
    <div role="img" aria-label={label}>
      <div aria-hidden="true">
        <div className="flex items-baseline justify-between gap-2">
          <span className={`${number} text-text`}>{home}%</span>
          {isTossUp(game.homeWinProb) && (
            <span className="rounded-md bg-accent-bg px-2 py-0.5 text-[11px] font-bold text-accent">
              ほぼ互角
            </span>
          )}
          {/* 敗者側にも --text-2 を使う。差は色の濃淡ではなくバーの幅と数値で伝える */}
          <span className={`${number} text-text-2`}>{away}%</span>
        </div>
        <div className="mt-1.5 flex h-2 overflow-hidden rounded-full bg-track">
          <div className="h-full bg-accent" style={{ width: `${home}%` }} />
        </div>
      </div>
    </div>
  );
}
