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
 */
export function ResultComparison({ result }: { result: ResultView }) {
  const { home, away } = percentPair(result.homeWinProb);
  const homeWon = result.homeScore > result.awayScore;

  return (
    <article className="rounded-2xl border border-border bg-surface p-4">
      <h3 className="text-[14px] font-bold">
        {result.home.name} <span className="text-text-2">対</span> {result.away.name}
      </h3>
      <dl className="mt-2 text-[13px]">
        <div className="flex items-baseline justify-between py-0.5">
          <dt className="text-text-2">実際のスコア</dt>
          <dd className="text-[19px] font-extrabold">
            {result.homeScore} <span className="text-text-3">–</span> {result.awayScore}
            <span className="ml-1 text-[12px] font-bold text-text-2">
              （{homeWon ? 'ホーム' : 'アウェイ'}勝利）
            </span>
          </dd>
        </div>
        <div className="flex items-baseline justify-between py-0.5">
          <dt className="text-text-2">予測</dt>
          <dd>
            ホーム {home}% / アウェイ {away}% ・ {result.predHomeScore}–{result.predAwayScore}
          </dd>
        </div>
        <div className="py-1">
          <dt className="text-text-2">判定</dt>
          <dd className="mt-0.5 leading-relaxed">
            {result.isCorrect
              ? `予測どおりでした（得点差の誤差 ${result.scoreError}点）`
              : `この試合は予測を外しました。ホーム${home}%と予想しましたが、${homeWon ? 'ホーム' : 'アウェイ'}が勝ちました。`}
          </dd>
        </div>
        <div className="py-1">
          <dt className="text-text-2">この予測の位置づけ</dt>
          <dd className="mt-0.5 leading-relaxed">
            {result.bucket.label}と予想した試合は、これまで{result.bucket.n}試合中
            {result.bucket.correct}試合（{(result.bucket.rate * 100).toFixed(1)}%）が的中しています。
          </dd>
        </div>
      </dl>
    </article>
  );
}
