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
//
// 2026-10-08 にスコアボード型へ移行した（要件 8.1 / 基本設計 6.1〜6.3）。
// **的中率は主数値である** — 26px 以上・等幅（`num`）・`--ink` で組み、母数を併記する。
// 区切りは罫線ではなく**面の明るさの差**で作る（`--panel` / `--panel-sub`）。

import { useEffect, useState } from 'react';
import { CalibrationChart, type CalibrationPoint } from '@/components/charts/CalibrationChart';
import { EmptyState } from '@/components/ui/EmptyState';
import { calibrationNote, correctCount } from '@/lib/map';
import { ACTIONS, LOADING, LOAD_ERROR, NO_ACCURACY_YET } from '@/lib/messages';
import { fetchAccuracy, type AccuracySummary } from '@/lib/source';

/** 率は小数第1位の百分率（詳細設計 5.5）。`%` は勝率専用である */
const pct = (value: number) => `${(value * 100).toFixed(1)}%`;

/** 節見出し。明朝16px・`--ink-2`（基本設計 6.1 / 6.2。`--ink` は主数値と h2 に予約する） */
function Heading({ children }: { children: React.ReactNode }) {
  return <h3 className="font-serif text-[16px] font-semibold text-ink-2">{children}</h3>;
}

/**
 * 1行＝1区分。**区切りは面の明るさの差で作る**（基本設計 6.3）。
 * 線を引かないため、行は交互に `--panel-sub` / `--panel` を敷く。
 *
 * **`dl` / `dt` / `dd` の対応は崩さない**（区分と的中率は見出しと値の対である。
 * 詳細設計 5.2）。
 */
function Row({ index, children }: { index: number; children: React.ReactNode }) {
  return (
    <div
      className={`flex min-h-13 items-center justify-between gap-3 rounded-xs px-3 py-2 ${
        index % 2 === 0 ? 'bg-panel-sub' : 'bg-panel'
      }`}
    >
      {children}
    </div>
  );
}

/** 的中率と母数。**母数を必ず併記する**（要件 8.3） */
function Rate({ accuracy, n }: { accuracy: number; n: number }) {
  return (
    <dd className="flex shrink-0 items-baseline gap-1.5">
      <span className="num text-[18px] leading-none text-ink">{pct(accuracy)}</span>
      <span className="num text-[12px] text-ink-3">（{n}試合）</span>
    </dd>
  );
}

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
        className="mt-4 rounded-xs bg-panel px-3 py-4 text-center text-[15px] text-ink-3"
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
      {/* 1. ベースライン比較を最初に置く（基本設計 5.2）。自分の成績だけを見せない。
          **文は設計が定めた形のまま出す**（基本設計 5.2 の並びの1番）。
          スコアボード型では**率そのものを主数値として立て**、母数と的中数は
          すぐ下の文が持つ */}
      <section className="mt-4 rounded-xs bg-panel px-3.5 py-3">
        <h3 className="text-[12px] font-bold tracking-[0.1em] text-ink-3">通算</h3>
        <p className="num mt-1.5 text-[34px] leading-none text-ink">{pct(overall.accuracy)}</p>
        <p className="mt-2.5 text-[15px] leading-relaxed text-ink-2">
          {overall.n}試合中 {correctCount(overall.accuracy, overall.n)}試合を的中（
          {pct(overall.accuracy)}）。
          {overall.baselineAccuracy === null
            ? // **推測で埋めない。** `baseline_accuracy` は OVERALL にだけ入る
              // （詳細設計 4.12）が、入っていなければ比較を書かない
              ''
            : `「ホームが必ず勝つ」と予想した場合は ${pct(overall.baselineAccuracy)} でした。`}
        </p>

        {/* 2. 予想スコアの誤差（基本設計 5.2。v1.53 で足した）。
            **1チームあたりの誤差であり、得点差の MAE とは別物である**（要件 6.4）。
            母数は勝敗と同じ `n` — 集計側が食い違いを見つけたときだけ null になる
            （詳細設計 4.12）。**null のときは行を出さない**（0 と書くと「誤差なし」
            の意味になる）。
            **仕切りは面の中なので罫線でよい**（`--rule` は装飾の仕切り。基本設計 6.1） */}
        {overall.scoreMae !== null && (
          <p className="mt-3 border-t border-rule pt-3 text-[15px] leading-relaxed text-ink-2">
            予想スコアは1チームあたり平均{' '}
            <span className="num text-[18px] text-ink">{overall.scoreMae.toFixed(1)}</span>
            点ずれています。
          </p>
        )}
      </section>

      {/* 3. シーズン別の推移 */}
      {summary.bySeason.length > 0 && (
        <section className="mt-6">
          <Heading>シーズン別</Heading>
          <dl className="mt-2 flex flex-col gap-0.5">
            {summary.bySeason.map((row, at) => (
              <Row key={row.seasonId} index={at}>
                <dt className="num min-w-0 truncate text-[15px] text-ink-2">{row.seasonId}</dt>
                <Rate accuracy={row.accuracy} n={row.n} />
              </Row>
            ))}
          </dl>
        </section>
      )}

      {/* 4. モデルバージョン別（受け入れ基準 A-05 / 要件 F-07）。
          **基本設計 5.2 の旧版の並びに入っていなかったが、A-05 が要求している** */}
      {summary.byModel.length > 0 && (
        <section className="mt-6">
          <Heading>モデルバージョン別</Heading>
          <dl className="mt-2 flex flex-col gap-0.5">
            {summary.byModel.map((row, at) => (
              <Row key={row.modelVersion} index={at}>
                <dt className="num min-w-0 truncate text-[14px] text-ink-2">{row.modelVersion}</dt>
                <Rate accuracy={row.accuracy} n={row.n} />
              </Row>
            ))}
          </dl>
        </section>
      )}

      {/* 5. 較正の言い換え（表）→ 較正曲線（グラフ）。
          **言い換えを先に置く**（基本設計 5.2）。専門指標をそのまま出さない */}
      {summary.calibration.length > 0 && (
        <section className="mt-6">
          <Heading>数字どおりに当たっているか</Heading>
          <dl className="mt-2 flex flex-col gap-0.5">
            {summary.calibration.map((point, at) => (
              <div
                key={point.bucket}
                className={`rounded-xs px-3 py-2.5 ${at % 2 === 0 ? 'bg-panel-sub' : 'bg-panel'}`}
              >
                <dt className="text-[12px] font-bold tracking-[0.04em] text-ink-3">
                  {point.bucket}くらいと予想した試合 {point.n}件
                </dt>
                <dd className="mt-1 text-[15px] leading-relaxed text-ink-2">
                  {point.actual === null ? (
                    // **0 を出さない。** 実績が無い帯は「勝っていない」ではない
                    <span className="text-ink-3">実績の集計がまだありません。</span>
                  ) : (
                    <>
                      実際に勝ったのは{' '}
                      <span className="num text-[18px] text-ink">{pct(point.actual)}</span>。
                      <span className="ml-1">{calibrationNote(point.predicted, point.actual)}</span>
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
          <Heading>出場選手の発表前と発表後</Heading>
          <p className="mt-2 rounded-xs bg-panel px-3.5 py-3 text-[15px] leading-relaxed text-ink-2">
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
          **Log Loss は出さない**（基本設計 5.2 の v1.55）— 要件 6.4 は Log Loss を
          「学習時の目的関数」と定めており、表示指標として挙げていない。
          `model_versions.cv_logloss` は学習当時のウィンドウの値で、別の量である */}
      <details className="mt-6 rounded-xs bg-panel">
        <summary className="flex min-h-11 cursor-pointer list-none items-center justify-between px-3.5 py-3 text-[15px] font-bold text-ink-2">
          詳しい指標
          <span aria-hidden="true" className="disclosure-marker text-[12px] text-ink-3">
            ▾
          </span>
        </summary>
        <div className="px-3.5 pb-3">
          {/* **`dl` の直下に `p` を置かない**（HTML が許すのは dt / dd / div である） */}
          <dl className="flex items-baseline justify-between gap-3 border-t border-rule pt-3">
            {/* 小数点前のゼロを省略しない（`.204` と書かない。詳細設計 5.5） */}
            <dt className="text-[15px] text-ink-2">Brier Score</dt>
            <dd className="num text-[20px] leading-none text-ink">{overall.brier.toFixed(3)}</dd>
          </dl>
          <p className="mt-2 text-[12px] leading-relaxed text-ink-3">
            0に近いほど良い指標です。確率が数字どおりで、かつ はっきり当てているほど
            小さくなります。
          </p>
        </div>
      </details>
    </>
  );
}
