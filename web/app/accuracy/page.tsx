import { CalibrationChart } from '@/components/charts/CalibrationChart';
import {
  SAMPLE_BY_PROVISIONAL,
  SAMPLE_BY_SEASON,
  SAMPLE_CALIBRATION,
  SAMPLE_OVERALL,
} from '@/lib/fixtures/accuracy';

export const dynamic = 'force-static';
export const metadata = { title: '的中率 | B.PREDICT（仮称）' };

/** 一般向けから専門向けへ、この順で並べる（詳細設計 5.2） */
export default function Page() {
  return (
    <>
      <h2 className="mt-5 text-[19px] font-extrabold">的中率</h2>

      <p className="mt-3 rounded-xl border border-border px-3 py-2 text-[11px] leading-relaxed text-text-2">
        これは表示を確認するための合成データです。実際の成績ではありません。
      </p>

      {/* ベースライン比較を最初に置く */}
      <p className="mt-4 text-[14px] leading-relaxed">
        {SAMPLE_OVERALL.n}試合中 {SAMPLE_OVERALL.correct}試合を的中（
        {(SAMPLE_OVERALL.accuracy * 100).toFixed(1)}%）。「ホームが必ず勝つ」と予想した場合は{' '}
        {(SAMPLE_OVERALL.baselineAccuracy * 100).toFixed(1)}% でした。
      </p>

      <section className="mt-6">
        <h3 className="text-[15px] font-extrabold">シーズン別</h3>
        <dl className="mt-2 rounded-xl border border-border bg-surface p-3">
          {SAMPLE_BY_SEASON.map((row) => (
            <div key={row.season} className="flex items-baseline justify-between py-0.5 text-[13px]">
              <dt className="text-text-2">{row.season}</dt>
              <dd>
                {(row.accuracy * 100).toFixed(1)}%
                <span className="text-text-2">（{row.n}試合）</span>
              </dd>
            </div>
          ))}
        </dl>
      </section>

      <section className="mt-6">
        <h3 className="text-[15px] font-extrabold">数字どおりに当たっているか</h3>
        {/* 専門指標の前に一般向けの言い換えを置く */}
        <dl className="mt-2 rounded-xl border border-border bg-surface p-3">
          {SAMPLE_CALIBRATION.map((point) => (
            <div key={point.bucket} className="py-1 text-[13px]">
              <dt className="text-text-2">
                {point.bucket}くらいと予想した試合 {point.n}件
              </dt>
              <dd>
                実際に勝ったのは {(point.actual * 100).toFixed(1)}%。
                <span className="text-text-2">
                  {point.actual < point.predicted ? '予想よりやや勝てていません。' : '予想どおりです。'}
                </span>
              </dd>
            </div>
          ))}
        </dl>
        <CalibrationChart points={SAMPLE_CALIBRATION} />
      </section>

      <section className="mt-6">
        <h3 className="text-[15px] font-extrabold">出場選手の発表前と発表後</h3>
        <p className="mt-2 text-[13px] leading-relaxed text-text-2">
          出場選手が未発表の段階で出した予測{' '}
          {(SAMPLE_BY_PROVISIONAL.provisional.accuracy * 100).toFixed(1)}%（
          {SAMPLE_BY_PROVISIONAL.provisional.n}試合）／確定後{' '}
          {(SAMPLE_BY_PROVISIONAL.confirmed.accuracy * 100).toFixed(1)}%（
          {SAMPLE_BY_PROVISIONAL.confirmed.n}試合）
        </p>
      </section>

      {/* Brier はトップに出さない。折りたたみの中に置く（詳細設計 5.2） */}
      <details className="mt-6 rounded-xl border border-border bg-surface">
        <summary className="flex min-h-11 cursor-pointer list-none items-center px-3 text-[13px] font-bold">
          詳しい指標
        </summary>
        <dl className="border-t border-border px-3 py-2 text-[13px]">
          <div className="flex items-baseline justify-between py-0.5">
            <dt className="text-text-2">Brier Score</dt>
            <dd>{SAMPLE_OVERALL.brier.toFixed(3)}</dd>
          </div>
          <div className="flex items-baseline justify-between py-0.5">
            <dt className="text-text-2">Log Loss</dt>
            <dd>{SAMPLE_OVERALL.logloss.toFixed(3)}</dd>
          </div>
          <p className="mt-1 text-[11px] text-text-3">どちらも0に近いほど良い指標です。</p>
        </dl>
      </details>
    </>
  );
}
