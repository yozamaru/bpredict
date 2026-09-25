import Link from 'next/link';

export type HistoryView = {
  gameId: string;
  /** 「9月20日」 */
  dateLabel: string;
  isHome: boolean;
  opponentName: string;
  /** **このクラブから見た**勝率 0〜1 */
  ownWinProb: number;
  ownScore: number;
  opponentScore: number;
  /**
   * 的中の判定は**サーバが出した値**を使う（`prediction_results.is_correct`）。
   * 画面でスコアから計算し直すと、中止・延期（VOID）の扱いが画面側に漏れる。
   */
  isCorrect: boolean;
};

/**
 * クラブ別ページの予測履歴の1行。
 *
 * **2行に分ける。** 1行に7項目を並べると 375px で対戦相手名の幅が 40px 未満になり、
 * ほぼ読めなくなる（コンテンツ幅の上限は 440px、左右ガター 16px。基本設計 6.3）。
 * 上段に「いつ・どこで・誰と」、下段に「予測と結果」を置く。
 *
 * **勝率は両チーム分を出す**（要件 8.3）。「このクラブの勝率」だけを出すと、
 * 誰の何%なのかが行だけでは読めない。圧縮表示でも省略しない。
 *
 * **外れた試合に強い否定色を使わない**（詳細設計 5.3）。的中は `--accent`、
 * 外れは `--text-2` にし、**色だけで情報を伝えない**（文字でも示す。要件 8.6）。
 */
export function HistoryRow({ item }: { item: HistoryView }) {
  const own = Math.round(item.ownWinProb * 100);
  const opponent = 100 - own;
  const won = item.ownScore > item.opponentScore;

  return (
    <li className="border-b border-border last:border-b-0">
      <Link href={`/games/${item.gameId}/`} className="block min-h-11 px-1 py-2">
        <div className="flex items-baseline gap-2 text-[13px]">
          <span className="w-14 shrink-0 text-text-2">{item.dateLabel}</span>
          <span className="w-11 shrink-0 text-[11px] font-bold text-text-2">
            {item.isHome ? 'ホーム' : 'アウェイ'}
          </span>
          <span className="min-w-0 flex-1 truncate font-bold">{item.opponentName}</span>
          <span className="shrink-0 font-bold">
            {item.ownScore}–{item.opponentScore}
          </span>
          <span className="w-7 shrink-0 text-right text-[11px] font-bold text-text-2">
            {won ? '勝ち' : '負け'}
          </span>
        </div>
        <div className="mt-0.5 flex items-baseline gap-2 pl-[6.5rem] text-[11px]">
          <span className="flex-1 text-text-3">
            予測 {own}% — {opponent}%
          </span>
          <span
            className={`shrink-0 font-bold ${item.isCorrect ? 'text-accent' : 'text-text-2'}`}
          >
            {item.isCorrect ? '的中' : '外れ'}
          </span>
        </div>
      </Link>
    </li>
  );
}
