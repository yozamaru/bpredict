'use client';

// 結果（基本設計 5.2 / 詳細設計 5.6）。
//
// **見る日は `meta.json` の `latestResultDate` で決める**（詳細設計 3.7）。
// **時計を見ない** — 静的配信は「いま」を知らず、時刻で変わる表示はキャッシュと
// 噛み合わない（`latestSeasonId` が時計を見ないのと同じ理由。詳細設計 3.3）。
//
// **前日を既定にしない。** 試合がない日・終わっていない日が多く、ほとんどの訪問で
// 空になる。バッチは「どの日を照合したか」を知っているので、それを書いてもらう。

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { ResultComparison, type ResultView } from '@/components/prediction/ResultComparison';
import { EmptyState } from '@/components/ui/EmptyState';
import { ACTIONS, LOADING, LOAD_ERROR, NO_ACCURACY_YET } from '@/lib/messages';
import { dateLabel, toResults } from '@/lib/map';
import { fetchMeta, fetchResults } from '@/lib/source';

type Loaded = { day: string; results: ResultView[] };

export function ResultsView() {
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [empty, setEmpty] = useState(false);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    fetchMeta()
      .then((meta) => {
        // **`?? null` で受ける。** 型は契約（詳細設計 3.7）を述べているが、
        // **配信中の `meta.json` はデプロイより古いことがある** — この画面を
        // 配った直後、次の `daily_ingest` が書くまでキーが無い。
        // `undefined` を日付として渡すと取得先が壊れる
        const date = meta.latestResultDate ?? null;
        // **1試合も照合していない間は null。** 「読み込み中」でも「エラー」でも
        // なく、集計する対象がまだ無いだけである（要件 8.5）
        if (date === null) {
          if (alive) setEmpty(true);
          return null;
        }
        return fetchResults(date).then((source) => {
          if (alive) {
            setLoaded({ day: source.gameDate, results: toResults(source) });
          }
          return null;
        });
      })
      .catch(() => {
        if (alive) setFailed(true);
      });
    return () => {
      alive = false;
    };
  }, []);

  return (
    <>
      <h2 className="mt-5 text-[19px] font-extrabold">結果</h2>

      {/* 外れた試合を隠さない（要件 8.3）。並びも扱いも的中と同じにする */}
      <p className="mt-2 text-[11px] leading-relaxed text-ink-3">
        予測を外した試合も同じ並びで出しています。それぞれに、その確率帯の通算成績を添えました。
      </p>

      {loaded !== null && (
        <h3 className="mt-3 text-[16px] font-bold">{dateLabel(loaded.day)}の結果</h3>
      )}

      <div className="mt-3 flex flex-col gap-3">
        {failed ? (
          <EmptyState message={LOAD_ERROR} action={ACTIONS.about} />
        ) : empty ? (
          // **照合した試合がまだない。** 本番で初めて走るのは最初の試合の後である
          <EmptyState message={NO_ACCURACY_YET} action={ACTIONS.today} />
        ) : loaded === null ? (
          <EmptyState message={LOADING} />
        ) : loaded.results.length === 0 ? (
          <EmptyState message={NO_ACCURACY_YET} action={ACTIONS.today} />
        ) : (
          loaded.results.map((result) => (
            <ResultComparison key={result.gameId} result={result} />
          ))
        )}
      </div>

      <Link
        href={ACTIONS.accuracy.href}
        className="mt-4 flex min-h-11 items-center justify-center rounded-xl border border-rule text-[13px] font-bold"
      >
        通算の的中率と較正を見る
      </Link>
    </>
  );
}
