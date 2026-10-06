// シーズン別・通算の集計の表（詳細設計 5.3）。
//
// **チームと選手で共用する。** どちらも「1行 = 1スコープ（通算 / 季）」で、
// 列は主要項目＋展開でボックススコアである。**2つ作らない** — 同じ表を2回実装すると、
// 率の併記や母数の扱いが片方だけ直る。
//
// **出すのは集計値だけである**（要件 3.1.1）。試合ごとの記録を並べない。
//
// **順位の数字を振らない。** ランキングにしない（要件 3.1.1）。

import type { StatBox } from '@/lib/source';

/** 1試合平均。**null なら「—」**（0 と書かない。0 は「0だった」を意味する）。 */
function num(value: number | null, digits = 1): string {
  return value === null ? '—' : value.toFixed(digits);
}

/**
 * 率は**必ず分数と併記する**（要件 8.3）。単独で `46.2%` と出さない。
 *
 * **実績に閾値を設けない。** `pct` が null なのは試投数が0のときだけで、
 * そのとき率は定義されない（詳細設計 1.9 / 3.3）。
 */
function Shooting({ value }: { value: { m: number | null; a: number | null; pct: number | null } }) {
  return (
    <span className="font-mono">
      {num(value.m)} / {num(value.a)}
      {value.pct !== null && (
        <span className="text-ink-2"> （{(value.pct * 100).toFixed(1)}%）</span>
      )}
    </span>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-2 py-0.5">
      <dt className="text-[11.5px] text-ink-2">{label}</dt>
      <dd className="text-[12.5px]">{children}</dd>
    </div>
  );
}

/** 展開で出すボックススコア（要件 8.3 の段階開示）。 */
function Box({ box, denominatorLabel }: { box: StatBox; denominatorLabel?: string }) {
  return (
    <dl className="border-t border-rule-soft px-2.5 py-2">
      {/* **母数が2つあるときは、どちらが何の母数かを書く**（詳細設計 5.3） */}
      {denominatorLabel !== undefined && (
        <p className="pb-1 text-[10.5px] text-ink-3">{denominatorLabel}</p>
      )}
      <Row label="FG">
        <Shooting value={box.fg} />
      </Row>
      <Row label="　2P">
        <Shooting value={box.fg2} />
      </Row>
      <Row label="　3P">
        <Shooting value={box.fg3} />
      </Row>
      <Row label="FT">
        <Shooting value={box.ft} />
      </Row>
      <Row label="リバウンド">
        <span className="font-mono">
          {num((box.oreb ?? 0) + (box.dreb ?? 0))}
          <span className="text-ink-2">
            {' '}
            （OR {num(box.oreb)} / DR {num(box.dreb)}）
          </span>
        </span>
      </Row>
      <Row label="アシスト">
        <span className="font-mono">{num(box.ast)}</span>
      </Row>
      <Row label="ターンオーバー">
        <span className="font-mono">{num(box.tov)}</span>
      </Row>
      <Row label="スティール">
        <span className="font-mono">{num(box.stl)}</span>
      </Row>
      <Row label="ブロック">
        <span className="font-mono">{num(box.blk)}</span>
      </Row>
      <Row label="ファウル">
        <span className="font-mono">{num(box.pf)}</span>
      </Row>
      <Row label="被ファウル">
        <span className="font-mono">{num(box.fd)}</span>
      </Row>
    </dl>
  );
}

/** 表の1行。`cells` は右寄せの数値列（主要項目）。 */
export type StatRow = {
  key: string;
  /** 「通算（2016-17 以降）」「2026-27 <クラブ>」など */
  label: string;
  /** **母数。必ず同じ行に出す**（要件 8.3。1試合平均は母数なしで読めない） */
  games: number;
  cells: (number | null)[];
  box: StatBox;
  /** 母数が2つあるクラブ側だけ渡す */
  denominatorLabel?: string;
};

export function StatSummaryTable({
  heading,
  columns,
  rows,
}: {
  heading: string;
  /** 主要項目の見出し（`cells` と同じ数・同じ順） */
  columns: readonly string[];
  rows: readonly StatRow[];
}) {
  return (
    <section className="mt-5">
      <h3 className="font-serif text-[16px] font-semibold">{heading}</h3>
      <div className="mt-2 border border-rule bg-panel">
        <div
          aria-hidden="true"
          className="flex items-baseline gap-2 border-b border-rule px-2.5 py-1 text-[9.5px] tracking-[0.06em] text-ink-3"
        >
          <span className="flex-1">区分</span>
          <span className="w-12 text-right">試合</span>
          {columns.map((name) => (
            <span key={name} className="w-12 text-right">
              {name}
            </span>
          ))}
          <span className="w-3" />
        </div>
        <ul className="list-none p-0">
          {rows.map((row) => (
            <li key={row.key} className="border-b border-rule-soft last:border-b-0">
              <details>
                <summary className="flex min-h-11 cursor-pointer list-none items-baseline gap-2 px-2.5 py-2">
                  <span className="flex-1 truncate text-[12.5px] font-bold">{row.label}</span>
                  <span className="w-12 text-right font-mono text-[12.5px]">
                    {row.games}
                  </span>
                  {row.cells.map((value, at) => (
                    <span
                      // 列の見出しと対応する（並びが固定である）
                      key={columns[at] ?? String(at)}
                      className="w-12 text-right font-mono text-[12.5px]"
                    >
                      {num(value)}
                    </span>
                  ))}
                  <span
                    aria-hidden="true"
                    className="disclosure-marker w-3 text-right text-[11px] text-ink-2"
                  >
                    ▾
                  </span>
                </summary>
                <Box box={row.box} denominatorLabel={row.denominatorLabel} />
              </details>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}
