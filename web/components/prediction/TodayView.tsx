'use client';

// 今日の予測（基本設計 5.2 / 詳細設計 5.6）。
//
// **クライアントで静的JSON を読む**（基本設計 5.6）。ビルド時に埋め込むと、
// バッチが1日4回書き換えるたびに再ビルドが要る。
//
// **`page.tsx` から切り出してある。** ルートは静的出力のままにし、
// クライアントの JS をこの木だけに閉じる（初期JS の予算は gzip 180KB。要件 4.2）。

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { GameTable } from '@/components/prediction/GameTable';
import { StaleBanner } from '@/components/prediction/StaleBanner';
import { EmptyState } from '@/components/ui/EmptyState';
import { ACTIONS, LOADING, LOAD_ERROR, noGames } from '@/lib/messages';
import {
  dateLabel,
  generatedAtLabel,
  isStale,
  shiftDate,
  toAccuracy,
  toGames,
} from '@/lib/map';
import { fetchMeta, fetchToday, type GamesByDate, type Meta } from '@/lib/source';
import { byTipoff, isTossUp, type AccuracyView, type GameView } from '@/lib/view';

/** 「ホームが必ず勝つ」の的中率。**API が返すまで併記できない**（下記）。 */
type Loaded = {
  day: string;
  games: GameView[];
  withoutPrediction: number;
  accuracy: AccuracyView | null;
  meta: Meta;
};

export function TodayView() {
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    // **一覧と `meta.json` を同時に取る。** 片方だけ先に出すと、遅延バナーが
    // 後から現れて表が飛ぶ（CLS。要件 4.2 で 0.1 以内）
    Promise.all([fetchToday(), fetchMeta()])
      .then(([list, meta]: [GamesByDate, Meta]) => {
        if (!alive) return;
        const games = toGames(list);
        setLoaded({
          day: list.gameDate,
          games,
          withoutPrediction: list.games.length - games.length,
          // **ベースラインが来るまで的中率を出さない。** 自分の成績だけを
          // 見せないのが要件 8.3 の条件である（同じ大きさで隣に置く）
          accuracy: toAccuracy(list.accuracy, null),
          meta,
        });
      })
      .catch(() => {
        if (alive) setFailed(true);
      });
    return () => {
      alive = false;
    };
  }, []);

  if (failed) {
    return (
      <div className="mt-3">
        <EmptyState message={LOAD_ERROR} action={ACTIONS.about} />
      </div>
    );
  }
  if (loaded === null) {
    // **エラーと同じ文言を使い回さない**（取得中と失敗が区別できなくなる）
    return (
      <p
        aria-live="polite"
        className="mt-3 border border-dashed border-rule px-2 py-3 text-center text-[12.5px] text-ink-3"
      >
        {LOADING}
      </p>
    );
  }

  const label = dateLabel(loaded.day);
  const games = [...loaded.games].sort(byTipoff);
  const tossUps = games.filter((game) => isTossUp(game.homeWinProb));

  return (
    <>
      {isStale(loaded.meta) && (
        <StaleBanner generatedAtLabel={generatedAtLabel(loaded.meta.generatedAt)} />
      )}

      <div className="mt-4 flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <h2 className="font-serif text-[19px] font-semibold tracking-[0.02em]">
          {label}の予測
        </h2>
        <span className="font-mono text-[11px] text-ink-2">全{games.length}試合</span>
      </div>

      {/* **日付別へ辿れるようにする**（基本設計 5.1）。導線がないと URL を
          手で打つ以外に他の日を見る方法がない。**窓は当日＋7日**（詳細設計 3.7） */}
      <nav aria-label="日付の移動" className="mt-2.5 flex items-stretch gap-2">
        <Link
          href={`/schedule/${shiftDate(loaded.day, -1)}/`}
          className="flex min-h-11 flex-1 items-center justify-center border border-rule text-[13px] font-bold"
        >
          ← 前日
        </Link>
        <Link
          href={`/schedule/${shiftDate(loaded.day, 1)}/`}
          className="flex min-h-11 flex-1 items-center justify-center border border-rule text-[13px] font-bold"
        >
          {dateLabel(shiftDate(loaded.day, 1))} →
        </Link>
      </nav>

      {games.length > 0 ? (
        <>
          <GameTable games={games} dateLabel={label} />
          <ul className="mt-2 list-none space-y-0.5 p-0 text-[10.5px] leading-relaxed text-ink-3">
            {tossUps.length > 0 && (
              <li className="relative pl-[11px] before:absolute before:left-0 before:text-ink-3 before:content-['—']">
                <b className="font-bold text-ink-2">ほぼ互角の{tossUps.length}試合</b>
                （{tossUps.map((game) => game.tipoffLabel ?? '時刻未定').join('・')}）は、
                モデルがどちらとも言えていない試合です。数字の大きい試合と同じ確からしさでは読めません。
              </li>
            )}
            <li className="relative pl-[11px] before:absolute before:left-0 before:text-ink-3 before:content-['—']">
              予想スコアは<b className="font-bold text-ink-2">整数</b>で表示します。
              1試合あたりの平均誤差が8〜10点あるため、小数第1位は精度の誤認を招きます。
            </li>
            {loaded.withoutPrediction > 0 && (
              // **予測がない試合を黙って消さない**（要件 8.5 の「予測未生成」）
              <li className="relative pl-[11px] before:absolute before:left-0 before:text-ink-3 before:content-['—']">
                この日の<b className="font-bold text-ink-2">{loaded.withoutPrediction}試合</b>
                は、まだ予測が公開されていません。
              </li>
            )}
          </ul>
        </>
      ) : (
        <div className="mt-3">
          <EmptyState message={noGames(label)} action={ACTIONS.accuracy} />
        </div>
      )}
    </>
  );
}
