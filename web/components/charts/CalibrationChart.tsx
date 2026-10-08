// チャートはライブラリを使わずインライン SVG で実装する（要件 8.1）。
// Recharts などを入れると /accuracy だけで gzip 後 100〜300KB 増える。
export type CalibrationPoint = { bucket: string; predicted: number; actual: number; n: number };

/**
 * 較正曲線。
 *
 * **地（`--ground`）の上に直接置く。** パネルを敷かない — 対角線は `--axis` で、
 * `scripts/check-contrast.mjs` は `--axis` を **`--band` / `--groove` / `--ground`**
 * の3つの地に対して 3:1 で検査する（基本設計 6.4）。面を1つ挟むと、
 * **検査している組と画面で使う組がずれる**。
 */
export function CalibrationChart({ points }: { points: CalibrationPoint[] }) {
  const size = 200;
  const pad = 24;
  const scale = (value: number) => pad + value * (size - pad * 2);

  return (
    <figure className="mt-3">
      <svg
        viewBox={`0 0 ${size} ${size}`}
        className="w-full"
        role="img"
        aria-label="予想した勝率と実際に勝った割合の関係。対角線に近いほど数字どおりに当たっている"
      >
        {/* 色の役割は基本設計 6.1 / 6.4 のトークン表に合わせる。
            線はテキストとは別の系列から選ぶ（--rule / --axis）。

            枠の軸線は **装飾的な仕切り**（--rule）。同じ情報を目盛りのラベルが
            伝えるため、3:1 を課さない。対角線を --axis にしてあるので、
            枠を同じ濃さで描くと**比べる相手がどちらか分からなくなる**。 */}
        <g className="stroke-rule" strokeWidth="1">
          <line x1={pad} y1={size - pad} x2={size - pad} y2={size - pad} />
          <line x1={pad} y1={pad} x2={pad} y2={size - pad} />
        </g>
        {/* 対角線は **意味を持つ線**。「この線に近いほど数字どおりに当たっている」
            という判定基準そのものであり、勝率バーの50%軸と同じ役割なので --axis を
            使う。地（--ground）に対し 3:1 以上を満たす（check-contrast.mjs が
            `['--axis', '--ground']` として固定する） */}
        <line
          x1={pad}
          y1={size - pad}
          x2={size - pad}
          y2={pad}
          className="stroke-axis"
          strokeWidth="1"
          strokeDasharray="3 3"
        />
        {/* 点はこの図の主役なので、主数値と同じ --ink を使う。
            **--home / --away は使わない** — あの2色は「ホーム側／アウェイ側」を
            指す対であり、較正の点にその意味はない。片方を借りると、無い区別を
            読ませることになる（基本設計 6.4）。 */}
        {points.map((point) => (
          <circle
            key={point.bucket}
            cx={scale(point.predicted)}
            cy={size - scale(point.actual)}
            r={4}
            className="fill-ink"
          />
        ))}
        {/* 文字はテキストの系列から選ぶ。ラベル・注記は --ink-3（図の下の
            figcaption と同じ）。線の系列を文字に使わない（基本設計 6.1 / 6.4）。
            **目盛りの数値は等幅で組む**（`num`。基本設計 6.2）が、
            軸の名前は本文の書体のままにする — 数値ではない */}
        <g className="fill-ink-3" fontSize="9">
          <text x={pad} y={size - pad + 11} className="num">
            0%
          </text>
          <text x={size - pad - 14} y={size - pad + 11} className="num">
            100%
          </text>
          <text x={2} y={pad + 4} className="num">
            100%
          </text>
          {/* 軸は日本語で書く（ui-implementation スキル） */}
          <text x={size / 2} y={size - 2} textAnchor="middle">
            予想した勝率
          </text>
          <text x={8} y={size / 2} textAnchor="middle" transform={`rotate(-90 8 ${size / 2})`}>
            実際に勝った割合
          </text>
        </g>
      </svg>
      <figcaption className="mt-2 text-[12px] leading-relaxed text-ink-3">
        横軸が予想した勝率、縦軸が実際に勝った割合です。点線に近いほど、数字どおりに当たっています。
      </figcaption>
    </figure>
  );
}
