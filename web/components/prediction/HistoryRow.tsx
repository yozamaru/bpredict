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
 * **勝率は両チーム分を出す**（要件 8.3）。「このクラブの勝率」だけを出すと、
 * 誰の何%なのかが行だけでは読めない。圧縮表示でも省略しない。
 *
 * **外れた試合に強い否定色を使わない**（詳細設計 5.3）。的中は `--accent`、
 * 外れは `--text-2` の記号で示し、色だけで情報を伝えない（○ / × の文字を添える）。
 */
export function HistoryRow({ item }: { item: HistoryView }) {
  const own = Math.round(item.ownWinProb * 100);
  const opponent = 100 - own;
  const won = item.ownScore > item.opponentScore;

  return (
    <li>
      <Link
        href={`/games/${item.gameId}/`}
        className="flex min-h-11 items-center gap-2 border-b border-border px-1 py-2 text-[13px] last:border-b-0"
      >
        <span className="w-14 shrink-0 text-text-2">{item.dateLabel}</span>
        <span className="w-8 shrink-0 text-[11px] font-bold text-text-2">
          {item.isHome ? 'ホーム' : 'アウェイ'}
        </span>
        <span className="min-w-0 flex-1 truncate">{item.opponentName}</span>
        <span className="shrink-0 tabular-nums text-text-2">
          {own}% — {opponent}%
        </span>
        <span className="w-14 shrink-0 text-right font-bold tabular-nums">
          {item.ownScore}–{item.opponentScore}
        </span>
        <span
          className={`w-8 shrink-0 text-right text-[11px] font-bold ${
            item.isCorrect ? 'text-accent' : 'text-text-2'
          }`}
        >
          {/* 色だけで伝えない。文字でも示す（要件 8.6） */}
          {item.isCorrect ? '的中' : '外れ'}
        </span>
        {/* **見えている文字を読み上げで繰り返さない**（詳細設計 5.2 と同じ理由）。
            「的中 / 外れ」は上に出ているため、ここは勝敗だけを補う */}
        <span className="sr-only">{won ? '勝ち' : '負け'}</span>
      </Link>
    </li>
  );
}
