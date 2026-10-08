import type { ReasonView } from '@/lib/view';

/**
 * 根拠は**要因グループ単位**で示す（要件 6.9）。
 * - 寄与の数値を画面に出さない。方向と相対的な強さ（3〜4段階のバー）で示す
 * - `%` は勝率専用。寄与はログオッズ空間の値であり確率への加算ではない
 * - 生の特徴量名を出さない
 *
 * **1件 = 1枚のボード**にする（2026-10-08。スコアボード型。基本設計 6.3）。
 * 囲みの罫線をやめ、`--panel` が地（`--ground`）から浮くことで1件ずつに見せる。
 *
 * **ラベルと有利な側を大きく、値はそれより小さくする。** 読み手が最初に
 * 知りたいのは「何が」「どちら側に」効いたかであって、その大きさではない
 * （大きさは下のバーが4段階で示す）。
 */
export function ReasonList({ summary, reasons }: { summary: string; reasons: ReasonView[] }) {
  return (
    <section className="mt-6">
      <h3 className="font-serif text-[16px] font-semibold text-ink-2">なぜこの予測になったか</h3>
      <p className="mt-1.5 text-[15px] leading-relaxed text-ink-2">{summary}</p>
      <ul className="mt-2.5 flex flex-col gap-2">
        {reasons.map((reason) => (
          <li key={reason.label} className="rounded-[2px] bg-panel px-3.5 py-3">
            <div className="flex items-baseline justify-between gap-2.5">
              <span className="min-w-0 flex-1 text-[15px] font-semibold text-ink-2">
                {reason.label}
              </span>
              {/* 色だけで伝えない。「ホーム有利」という文字がそのまま向きである（要件 8.6） */}
              <span
                className={`shrink-0 text-[15px] font-bold ${
                  reason.favors === 'HOME' ? 'text-home' : 'text-away'
                }`}
              >
                {reason.favors === 'HOME' ? 'ホーム有利' : 'アウェイ有利'}
              </span>
            </div>
            {/* 値はサーバが単位まで組んだ文字列。画面で組み立て直さない（詳細設計 2.7.1） */}
            <p className="num mt-1 text-[12px] text-ink-3">{reason.value}</p>
            {/* 寄与の生値は出さない。4段階の目盛りだけを出す（詳細設計 3.3） */}
            <div
              className="mt-2.5 flex gap-1"
              role="img"
              aria-label={`影響の大きさ 4段階のうち ${reason.strength}`}
            >
              {[1, 2, 3, 4].map((step) => (
                <span
                  key={step}
                  className={`h-1.5 flex-1 rounded-[1px] ${
                    step <= reason.strength
                      ? reason.favors === 'HOME'
                        ? 'bg-home'
                        : 'bg-away'
                      : 'bg-groove'
                  }`}
                />
              ))}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
