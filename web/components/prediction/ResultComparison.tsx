import { percentPair } from '@/lib/view';

export type ResultView = {
  gameId: string;
  home: { name: string };
  away: { name: string };
  homeScore: number;
  awayScore: number;
  homeWinProb: number;
  predHomeScore: number;
  predAwayScore: number;
  /**
   * 判定と誤差は**サーバが出した値**を使う（ui-implementation スキル）。
   * 的中の判定は `is_final = 1` の行に対する照合結果（`prediction_results`）であり、
   * 画面でスコアから計算し直すと、中止・延期（VOID）の扱いが画面側の実装に漏れる。
   */
  isCorrect: boolean;
  /** 得点差の誤差 */
  scoreError: number;
  /** その確率帯の通算成績。外れた試合でも必ず出す（要件 8.3） */
  bucket: { label: string; n: number; correct: number; rate: number };
};

/**
 * 試合後の対比。**このプロダクトで最も信頼が揺れる場面**であり、外れた試合を隠さない。
 * 外れに強い否定色を使わない（詳細設計 5.3）。
 *
 * **実績のスコアをボードの主数値に置く**（2026-10-08。スコアボード型）。
 * 無彩のまま 34px で出す — 有彩色は勝率だけに予約してある（基本設計 6.1 の
 * 「彩度の孤立」）。囲みの罫線はやめ、`--panel` が地から浮くことで1枚に見せる。
 */
export function ResultComparison({ result }: { result: ResultView }) {
  const { home, away } = percentPair(result.homeWinProb);
  const homeWon = result.homeScore > result.awayScore;

  return (
    <article className="rounded-[2px] bg-panel px-3.5 py-3.5">
      <h3 className="font-serif text-[16px] font-semibold text-ink-2">
        {result.home.name} <span className="text-ink-3">対</span> {result.away.name}
      </h3>

      <div className="mt-2.5 flex items-end justify-between gap-2">
        <span className="shrink-0 text-[12px] font-bold tracking-[0.08em] text-ink-3">
          実際のスコア
        </span>
        <span className="num text-[34px] leading-none text-ink">
          <span className="sr-only">ホーム </span>
          {result.homeScore} <span className="text-ink-3">–</span>
          <span className="sr-only"> アウェイ </span> {result.awayScore}
        </span>
      </div>
      <p className="mt-1 text-right text-[12px] font-bold text-ink-3">
        {homeWon ? 'ホーム' : 'アウェイ'}勝利
      </p>

      <div className="mt-3 h-px bg-rule" />

      <dl className="mt-2.5 text-[15px] leading-relaxed text-ink-2">
        <div className="flex items-baseline justify-between gap-3 py-[3px]">
          <dt className="shrink-0 text-[12px] text-ink-3">予測</dt>
          {/* 勝率は必ず両チーム分（要件 8.3）。予想スコアは整数 */}
          <dd className="num text-right">
            ホーム {home}% / アウェイ {away}% ・ {result.predHomeScore}–{result.predAwayScore}
          </dd>
        </div>
        <div className="py-1.5">
          <dt className="text-[12px] text-ink-3">判定</dt>
          <dd className="mt-0.5">
            {result.isCorrect
              ? `予測どおりでした（得点差の誤差 ${result.scoreError}点）`
              : `この試合は予測を外しました。ホーム${home}%と予想しましたが、${homeWon ? 'ホーム' : 'アウェイ'}が勝ちました。`}
          </dd>
        </div>
        {/* 外れを認めたうえで、較正が取れていること自体を信頼の材料として示す */}
        <div className="py-1.5">
          <dt className="text-[12px] text-ink-3">この予測の位置づけ</dt>
          <dd className="mt-0.5">
            {result.bucket.label}と予想した試合は、これまで{result.bucket.n}試合中
            {result.bucket.correct}試合（{(result.bucket.rate * 100).toFixed(1)}%）が的中しています。
          </dd>
        </div>
      </dl>
    </article>
  );
}
