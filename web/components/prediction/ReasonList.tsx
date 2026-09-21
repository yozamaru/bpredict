import type { ReasonView } from '@/lib/view';

/**
 * 根拠は**要因グループ単位**で示す（要件 6.9）。
 * - 寄与の数値を画面に出さない。方向と相対的な強さ（3〜4段階のバー）で示す
 * - `%` は勝率専用。SHAP はログオッズ空間の値であり確率への加算ではない
 * - 生の特徴量名を出さない
 */
export function ReasonList({ summary, reasons }: { summary: string; reasons: ReasonView[] }) {
  return (
    <section className="mt-6">
      <h3 className="text-[15px] font-extrabold">なぜこの予測になったか</h3>
      <p className="mt-1.5 text-[13px] leading-relaxed text-text-2">{summary}</p>
      <ul className="mt-3 flex flex-col gap-3">
        {reasons.map((reason) => (
          <li key={reason.label} className="rounded-xl border border-border bg-surface p-3">
            <div className="flex items-baseline justify-between gap-2">
              <span className="text-[13px] font-bold">{reason.label}</span>
              <span className="text-[11px] font-bold text-text-2">
                {reason.favors === 'HOME' ? 'ホーム有利' : 'アウェイ有利'}
              </span>
            </div>
            <p className="mt-0.5 text-[13px] text-text-2">{reason.value}</p>
            <div
              className="mt-2 flex gap-1"
              role="img"
              aria-label={`影響の大きさ 4段階のうち ${reason.strength}`}
            >
              {[1, 2, 3, 4].map((step) => (
                <span
                  key={step}
                  className={`h-1.5 flex-1 rounded-full ${
                    step <= reason.strength ? 'bg-accent' : 'bg-track'
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
