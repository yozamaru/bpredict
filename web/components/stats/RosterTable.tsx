// 当季の選手一覧（詳細設計 5.3）。
//
// **出典は `player_stat_summary`** であって `player_seasons` ではない（詳細設計 3.3）。
// あの断面は1試合も出ていない選手を含み、季中に離脱した選手が消えている。
//
// **ランキングの見た目にしない。** 順位の数字を振らない（要件 3.1.1）。並びは
// 読みやすさのためであって序列の主張ではない。
//
// 2026-10-08 にスコアボード型へ移した（基本設計 6.1〜6.3）。**区切りは面の差**で
// 作り（`--rule-soft` は廃止された）、**ポジションと背番号を2行目へ下げた** —
// 1行に7項目を並べると 375px で選手名の幅が削られる。

import Link from 'next/link';
import type { RosterEntry } from '@/lib/source';

function num(value: number | null, digits = 1): string {
  return value === null ? '—' : value.toFixed(digits);
}

/** 見出しの行と中身の行で**同じ定義を使う**（別に書くと列がずれる） */
const TEMPLATE = 'minmax(0,1fr) 32px 44px 44px 36px 36px';

export function RosterTable({
  entries,
  canLink,
}: {
  entries: readonly RosterEntry[];
  /** 静的生成の範囲内かどうか。**範囲外にはリンクを張らない**（要件 8.2） */
  canLink: (playerId: string) => boolean;
}) {
  return (
    <section className="mt-6">
      <h3 className="font-serif text-[16px] font-semibold text-ink-2">今季の選手</h3>
      <p className="mt-1 text-[12px] leading-relaxed text-ink-3">
        出場した選手を、出場時間の多い順に並べています。数値は1試合平均です。
      </p>
      <div className="mt-3">
        {/* **見出しは構造の帯**（`--strip`。基本設計 6.1 の役割表） */}
        <div
          aria-hidden="true"
          className="mb-0.5 grid items-baseline gap-1 rounded-xs bg-strip px-3 py-1.5 text-[12px] font-bold tracking-[0.06em] text-ink-3"
          style={{ gridTemplateColumns: TEMPLATE }}
        >
          <span>選手</span>
          <span className="text-right">試合</span>
          <span className="text-right">分</span>
          <span className="text-right">得点</span>
          <span className="text-right">R</span>
          <span className="text-right">A</span>
        </div>
        <ul className="flex list-none flex-col gap-0.5 p-0">
          {entries.map((entry, at) => {
            const linked = canLink(entry.playerId);
            const body = (
              <>
                <span className="flex min-w-0 flex-col gap-0.5">
                  {/* **どの行が開けるのかを下線で示す。** 色や位置だけに頼らない */}
                  <span
                    className={`truncate text-[15px] text-ink-2${
                      linked ? ' underline underline-offset-2' : ''
                    }`}
                  >
                    {entry.name}
                  </span>
                  {/* 未登録は「—」。**空にしない** — 何が出ない行なのか読めない */}
                  <span className="truncate text-[12px] text-ink-3">
                    {entry.position ?? '—'}
                    {entry.number !== null && <span className="num"> #{entry.number}</span>}
                  </span>
                </span>
                <span className="num text-right text-[15px] text-ink-2">{entry.games}</span>
                <span className="num text-right text-[16px] text-ink">
                  {num(entry.perGame.minutes)}
                </span>
                <span className="num text-right text-[16px] text-ink">
                  {num(entry.perGame.pts)}
                </span>
                <span className="num text-right text-[15px] text-ink-2">
                  {num(entry.perGame.reb)}
                </span>
                <span className="num text-right text-[15px] text-ink-2">
                  {num(entry.perGame.ast)}
                </span>
              </>
            );
            const className = 'grid min-h-13 items-center gap-1 px-3 py-2';
            return (
              <li
                key={entry.playerId}
                // **区切りは面の明るさの差で作る**（基本設計 6.3）。線は引かない
                className={`rounded-xs ${at % 2 === 0 ? 'bg-panel-sub' : 'bg-panel'}`}
              >
                {linked ? (
                  <Link
                    href={`/players/${entry.playerId}/`}
                    className={className}
                    style={{ gridTemplateColumns: TEMPLATE }}
                  >
                    {body}
                  </Link>
                ) : (
                  // **開けないリンクを置かない**（静的生成の範囲外。要件 8.2）
                  <div className={className} style={{ gridTemplateColumns: TEMPLATE }}>
                    {body}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      </div>
    </section>
  );
}
