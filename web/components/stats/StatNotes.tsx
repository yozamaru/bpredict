// 戦績・スタッツの注記（詳細設計 5.3）。
//
// **注記は設計の一部であり、消えても画面は壊れない。** `npm run test:stats` が
// 原文を読んで存在を検査する（`check-notes.mjs` と同じ理由）。

/** 通算の範囲（要件 3.1.1）。**リーグの歴史全体ではない。** */
export const CAREER_RANGE =
  '通算は 2016-17 シーズン以降（前身の B1 を含む）の、本サイトが取り込んだ記録です。';

/**
 * 不戦敗を含まないこと（要件 5.3 が「画面に注釈として出す」と定めたもの）。
 *
 * 取り込み自体をしていないため、**公式の順位表と勝敗数が食い違う**。
 */
export const FORFEIT_EXCLUDED =
  '不戦敗として記録された試合は含みません。公式の順位表と勝敗数が食い違う場合があります。';

/**
 * 選手個人への評価と読まれないようにする（要件 6.9 / R-11）。
 *
 * **個人スタッツ予測の固定注記と文言を揃えない** — あちらは「統計的推定値」であり、
 * ここは**実績**である。混ぜると実績を推定値だと読ませることになる。
 */
export const ACTUAL_NOT_RATING =
  '本サイトが公式記録から集計した実績値です。選手の能力や評価を示すものではありません。';

export function StatNotes({ notes }: { notes: readonly string[] }) {
  return (
    // 注記は 12px（基本設計 6.2。旧版は 10.5px で、運営者が「文字が小さい」と
    // 指摘した水準である）。**文言は1字も変えない** — `npm run test:stats` が照合する
    <ul className="mt-4 flex list-none flex-col gap-1.5 p-0 text-[12px] leading-relaxed text-ink-3">
      {notes.map((note) => (
        <li key={note}>{note}</li>
      ))}
    </ul>
  );
}
