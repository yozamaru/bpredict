// チャートはライブラリを使わずインライン SVG で実装する（要件 8.1）。
// Recharts などを入れると /accuracy だけで gzip 後 100〜300KB 増える。
export type CalibrationPoint = { bucket: string; predicted: number; actual: number; n: number };

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
        {/* 罫線は非テキストなので --text-4 を stroke で使う。
            文字（目盛りのラベル）には本文用の --text-3 を使う（基本設計 6.1） */}
        <g className="stroke-text-4" strokeWidth="1">
          <line x1={pad} y1={size - pad} x2={size - pad} y2={size - pad} />
          <line x1={pad} y1={pad} x2={pad} y2={size - pad} />
        </g>
        <line
          x1={pad}
          y1={size - pad}
          x2={size - pad}
          y2={pad}
          className="stroke-text-3"
          strokeWidth="1"
          strokeDasharray="3 3"
        />
        {points.map((point) => (
          <circle
            key={point.bucket}
            cx={scale(point.predicted)}
            cy={size - scale(point.actual)}
            r={3.5}
            className="fill-accent"
          />
        ))}
        <g className="fill-text-3" fontSize="8">
          <text x={pad} y={size - pad + 10}>
            0%
          </text>
          <text x={size - pad - 12} y={size - pad + 10}>
            100%
          </text>
          <text x={2} y={pad + 4}>
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
      <figcaption className="mt-1 text-[11px] leading-relaxed text-text-3">
        横軸が予想した勝率、縦軸が実際に勝った割合です。点線に近いほど、数字どおりに当たっています。
      </figcaption>
    </figure>
  );
}
