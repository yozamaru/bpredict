'use client';

// 的中率（要件 F-07 / 基本設計 5.2 / 詳細設計 3.3 の `/accuracy`）。
//
// **公開API から読む**（基本設計 2.5 の表）。通算の集計は静的JSON の窓
// （当日＋7日）に入らないため、ここは API が唯一の経路である。
//
// **一般向けから専門向けへ並べる**（基本設計 5.2）。利用者の大半は統計の
// 専門家ではないため、Brier をトップに出さない。
//
// **良い時も悪い時も同じ場所に出す**（要件 8.3）。良い数字だけを見せる分岐を
// 作らない。

import { useEffect, useState } from 'react';
import { CalibrationChart, type CalibrationPoint } from '@/components/charts/CalibrationChart';
import { EmptyState } from '@/components/ui/EmptyState';
import { calibrationNote, correctCount } from '@/lib/map';
import { ACTIONS, LOADING, LOAD_ERROR, NO_ACCURACY_YET } from '@/lib/messages';
import { fetchAccuracy, type AccuracySummary } from '@/lib/source';

/** 率は小数第1位の百分率（詳細設計 5.5）。`%` は勝率専用である */
const pct = (value: number) => `${(value * 100).toFixed(1)}%`;

export function AccuracyView() {
  const [summary, setSummary] = useState<AccuracySummary | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    // **取得は `lib/source.ts` を通す。** 例外の扱いとキャッシュ指定を1か所にする
    fetchAccuracy()
      .then((found) => {
        if (alive) setSummary(found);
      })
      .catch(() => {
        if (alive) setFailed(true);
      });
    return () => {
      alive = false;
    };
  }, []);

  if (failed) return <EmptyState message={LOAD_ERROR} action={ACTIONS.about} />;
  if (summary === null) {
    return (
      <p
        aria-live="polite"
        className="mt-5 border border-dashed border-rule px-2 py-3 text-center text-[12.5px] text-ink-3"
      >
        {LOADING}
      </p>
    );
  }

  // **`overall` が null は「取得に失敗した」ではない**（要件 8.5。v1.31 で表に
  // 加えた）。結果照合が初めて走るまで `accuracy_summary` は空である
  if (summary.overall === null || summary.overall.n <= 0) {
    return <EmptyState message={NO_ACCURACY_YET} action={ACTIONS.today} />;
  }

  const overall = summary.overall;
  // **較正グラフは実績が入った帯だけを描く。** `actual_rate` は NULL を取りうる
  // （詳細設計 1.6）。欠けた帯を 0 として打つと「1件も勝っていない」に見える
  const plotted: CalibrationPoint[] = summary.calibration
    .filter((point): point is typeof point & { actual: number } => point.actual !== null)
    .map(({ bucket, predicted, actual, n }) => ({ bucket, predicted, actual, n }));

  return (
    <>
      {/* 1. ベースライン比較を最初に置く（基本設計 5.2）。自分の成績だけを見せない */}
      <p className="mt-4 text-[14px] leading-relaxed">
        {overall.n}試合中 {correctCount(overall.accuracy, overall.n)}試合を的中（
        {pct(overall.accuracy)}）。
        {overall.baselineAccuracy === null
          ? // **推測で埋めない。** `baseline_accuracy` は OVERALL にだけ入る
            // （詳細設計 4.12）が、入っていなければ比較を書かない
            ''
          : `「ホームが必ず勝つ」と予想した場合は ${pct(overall.baselineAccuracy)} でした。`}
      </p>

      {/* 2. シーズン別の推移 */}
      {summary.bySeason.length > 0 && (
        <section className="mt-6">
          <h3 className="font-serif text-[16px] font-semibold">シーズン別</h3>
          <dl className="mt-2 border border-rule bg-panel">
            {summary.bySeason.map((row) => (
              <div
                key={row.seasonId}
                className="flex items-baseline justify-between border-b border-rule-soft px-2 py-1.5 text-[13px] last:border-b-0"
              >
                <dt className="text-ink-2">{row.seasonId}</dt>
                {/* 的中率には母数を併記する（要件 8.3） */}
                <dd className="font-mono tabular-nums">
                  {pct(row.accuracy)}
                  <span className="ml-1 text-[11px] text-ink-3">（{row.n}試合）</span>
                </dd>
              </div>
            ))}
          </dl>
        </section>
      )}

      {/* 3. モデルバージョン別（受け入れ基準 A-05 / 要件 F-07）。
          **基本設計 5.2 の旧版の並びに入っていなかったが、A-05 が要求している** */}
      {summary.byModel.length > 0 && (
        <section className="mt-6">
          <h3 className="font-serif text-[16px] font-semibold">モデルバージョン別</h3>
          <dl className="mt-2 border border-rule bg-panel">
            {summary.byModel.map((row) => (
              <div
                key={row.modelVersion}
                className="flex items-baseline justify-between border-b border-rule-soft px-2 py-1.5 text-[13px] last:border-b-0"
              >
                <dt className="font-mono text-[12px] text-ink-2">{row.modelVersion}</dt>
                <dd className="font-mono tabular-nums">
                  {pct(row.accuracy)}
                  <span className="ml-1 text-[11px] text-ink-3">（{row.n}試合）</span>
                </dd>
              </div>
            ))}
          </dl>
        </section>
      )}

      {/* 4. 較正の言い換え（表）→ 5. 較正曲線（グラフ）。
          **言い換えを先に置く**（基本設計 5.2）。専門指標をそのまま出さない */}
      {summary.calibration.length > 0 && (
        <section className="mt-6">
          <h3 className="font-serif text-[16px] font-semibold">数字どおりに当たっているか</h3>
          <dl className="mt-2 border border-rule bg-panel">
            {summary.calibration.map((point) => (
              <div
                key={point.bucket}
                className="border-b border-rule-soft px-2 py-1.5 text-[13px] last:border-b-0"
              >
                <dt className="text-[11px] text-ink-3">
                  {point.bucket}くらいと予想した試合 {point.n}件
                </dt>
                <dd className="mt-px">
                  {point.actual === null ? (
                    // **0 を出さない。** 実績が無い帯は「勝っていない」ではない
                    <span className="text-ink-3">実績の集計がまだありません。</span>
                  ) : (
                    <>
                      実際に勝ったのは{' '}
                      <span className="font-mono tabular-nums">{pct(point.actual)}</span>。
                      <span className="ml-1 text-ink-2">
                        {calibrationNote(point.predicted, point.actual)}
                      </span>
                    </>
                  )}
                </dd>
              </div>
            ))}
          </dl>
          {plotted.length > 0 && <CalibrationChart points={plotted} />}
        </section>
      )}

      {/* 6. 暫定/確定の内訳 */}
      {(summary.byProvisional.provisional !== null ||
        summary.byProvisional.confirmed !== null) && (
        <section className="mt-6">
          <h3 className="font-serif text-[16px] font-semibold">出場選手の発表前と発表後</h3>
          <p className="mt-2 text-[13px] leading-relaxed text-ink-2">
            {summary.byProvisional.provisional !== null && (
              <>
                出場選手が未発表の段階で出した予測{' '}
                {pct(summary.byProvisional.provisional.accuracy)}（
                {summary.byProvisional.provisional.n}試合）
              </>
            )}
            {summary.byProvisional.provisional !== null &&
              summary.byProvisional.confirmed !== null &&
              '／'}
            {summary.byProvisional.confirmed !== null && (
              <>
                確定後 {pct(summary.byProvisional.confirmed.accuracy)}（
                {summary.byProvisional.confirmed.n}試合）
              </>
            )}
          </p>
        </section>
      )}

      {/* 7. 詳しい指標（折りたたみ）。**Brier をトップに出さない**（基本設計 5.2）。
          **Log Loss は出せない** — `accuracy_summary` に列が無く、`/accuracy` は
          単一テーブルを読む設計である（基本設計 5.2 の「Log Loss は出せない」）。
          `model_versions.cv_logloss` は学習当時のウィンドウの値で、別の量である */}
      <details className="mt-6 border border-rule bg-panel">
        <summary className="flex min-h-11 cursor-pointer list-none items-center px-2 text-[13px] font-bold">
          詳しい指標
        </summary>
        <dl className="border-t border-rule px-2 py-1.5 text-[13px]">
          <div className="flex items-baseline justify-between py-0.5">
            {/* 小数点前のゼロを省略しない（`.204` と書かない。詳細設計 5.5） */}
            <dt className="text-ink-2">Brier Score</dt>
            <dd className="font-mono tabular-nums">{overall.brier.toFixed(3)}</dd>
          </div>
          <p className="mt-1 text-[11px] leading-relaxed text-ink-3">
            0に近いほど良い指標です。確率が数字どおりで、かつ はっきり当てているほど
            小さくなります。
          </p>
        </dl>
      </details>
    </>
  );
}
