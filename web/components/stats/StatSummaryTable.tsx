// シーズン別・通算の集計の表（詳細設計 5.3）。
//
// **チームと選手で共用する。** どちらも「1行 = 1スコープ（通算 / 季）」で、
// 列は主要項目＋展開でボックススコアである。**2つ作らない** — 同じ表を2回実装すると、
// 率の併記や母数の扱いが片方だけ直る。
//
// **出すのは集計値だけである**（要件 3.1.1）。試合ごとの記録を並べない。
//
// **順位の数字を振らない。** ランキングにしない（要件 3.1.1）。
//
// 2026-10-08 にスコアボード型へ移した（基本設計 6.1〜6.3）。変えたのは3点である。
//
// 1. **区切りを罫線から面の差にした**（`--rule-soft` は廃止された）。行は
//    `bg-panel` と `bg-panel-sub` を交互に敷き、間に 2px の地を覗かせる
// 2. **通算・合計だけ主数値を 26px のカードで出す**（基本設計 6.2）。
//    シーズン別は**横並びを残す** — 縦積みは季をまたいだ比較が原理的にできず、
//    「今季は去年より伸びているか」を記憶に頼らせる（基本設計 6.3）
// 3. **展開したフルボックススコア（20項目）だけ縦に積む**（375px に20列は入らない）

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
    <span className="num">
      {num(value.m)} / {num(value.a)}
      {value.pct !== null && (
        <span className="text-ink-3"> （{(value.pct * 100).toFixed(1)}%）</span>
      )}
    </span>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1">
      <dt className="text-[12px] font-bold tracking-[0.04em] text-ink-3">{label}</dt>
      <dd className="text-[15px] text-ink-2">{children}</dd>
    </div>
  );
}

/**
 * 展開で出すボックススコア（要件 8.3 の段階開示）。
 *
 * **面は `bg-panel-sub`**（従属情報の面。基本設計 6.1）。行の地と同じ明るさに
 * なる場合があるため、**装飾の仕切りを1本だけ上端に置く**（モックの区切り線と
 * 同じ扱いで、意味は持たない）。
 */
function Box({ box, denominatorLabel }: { box: StatBox; denominatorLabel?: string }) {
  return (
    <dl className="border-t border-rule bg-panel-sub px-3 py-2">
      {/* **母数が2つあるときは、どちらが何の母数かを書く**（詳細設計 5.3） */}
      {denominatorLabel !== undefined && (
        <p className="pb-1 text-[12px] text-ink-3">{denominatorLabel}</p>
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
        <span className="num">
          {num((box.oreb ?? 0) + (box.dreb ?? 0))}
          <span className="text-ink-3">
            {' '}
            （OR {num(box.oreb)} / DR {num(box.dreb)}）
          </span>
        </span>
      </Row>
      <Row label="アシスト">
        <span className="num">{num(box.ast)}</span>
      </Row>
      <Row label="ターンオーバー">
        <span className="num">{num(box.tov)}</span>
      </Row>
      <Row label="スティール">
        <span className="num">{num(box.stl)}</span>
      </Row>
      <Row label="ブロック">
        <span className="num">{num(box.blk)}</span>
      </Row>
      <Row label="ファウル">
        <span className="num">{num(box.pf)}</span>
      </Row>
      <Row label="被ファウル">
        <span className="num">{num(box.fd)}</span>
      </Row>
    </dl>
  );
}

/** 表の1行。`cells` は右寄せの数値列（主要項目）。 */
export type StatRow = {
  key: string;
  /** 「通算」「2026-27」など */
  label: string;
  /**
   * 2行目に小さく置く補足（季のクラブ名など）。
   *
   * **横幅を数値に譲るために2行にする。** 1行に収めると 375px で主数値の列が
   * 削られ、季をまたいだ比較ができなくなる（基本設計 6.3）。
   */
  sublabel?: string;
  /** **母数。必ず同じ行に出す**（要件 8.3。1試合平均は母数なしで読めない） */
  games: number;
  cells: (number | null)[];
  box: StatBox;
  /** 母数が2つあるクラブ側だけ渡す */
  denominatorLabel?: string;
  /** 通算・合計の行。**主数値を 26px のカードで出す**（基本設計 6.2） */
  total?: boolean;
};

/**
 * 通算・合計。**この画面で最も大きい数値である**（26px。基本設計 6.2）。
 *
 * **横並びの表に混ぜない。** 44px の列に 26px は入らず、混ぜると表の側が
 * 読めなくなる。カードにして主数値を立て、季の比較は下の表が担う。
 */
function TotalCard({ row, columns }: { row: StatRow; columns: readonly string[] }) {
  return (
    <details className="mt-2 rounded-xs bg-panel">
      <summary className="flex min-h-11 cursor-pointer list-none flex-col gap-2.5 px-3.5 py-3">
        <span className="flex items-baseline justify-between gap-2">
          <span className="text-[12px] font-bold tracking-[0.1em] text-ink-3">{row.label}</span>
          <span className="flex items-baseline gap-2">
            {/* **母数を同じ行に出す**（要件 8.3） */}
            <span className="num text-[15px] text-ink-2">{row.games}試合</span>
            <span aria-hidden="true" className="disclosure-marker text-[12px] text-ink-3">
              ▾
            </span>
          </span>
        </span>
        <span
          className="grid gap-2"
          style={{ gridTemplateColumns: `repeat(${columns.length}, minmax(0,1fr))` }}
        >
          {row.cells.map((value, at) => (
            // 列の見出しと対応する（並びが固定である）
            <span key={columns[at] ?? String(at)} className="flex flex-col gap-1">
              <span className="text-[12px] font-bold tracking-[0.04em] text-ink-3">
                {columns[at]}
              </span>
              <span className="num text-[26px] leading-none text-ink">{num(value)}</span>
            </span>
          ))}
        </span>
      </summary>
      <Box box={row.box} denominatorLabel={row.denominatorLabel} />
    </details>
  );
}

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
  // 見出しの行と中身の行で**同じ定義を使う**（別に書くと列がずれる）
  const template = `minmax(0,1fr) 32px repeat(${columns.length}, 44px) 12px`;
  const totals = rows.filter((row) => row.total === true);
  const seasons = rows.filter((row) => row.total !== true);

  return (
    <section className="mt-6">
      <h3 className="font-serif text-[16px] font-semibold text-ink-2">{heading}</h3>

      {/* **通算を先に出す**（基本設計 5.2）。まず全体の水準を示す */}
      {totals.map((row) => (
        <TotalCard key={row.key} row={row} columns={columns} />
      ))}

      {seasons.length !== 0 && (
        <div className="mt-3">
          {/* **見出しは構造の帯**（`--strip`。基本設計 6.1 の役割表） */}
          <div
            aria-hidden="true"
            className="mb-0.5 grid items-baseline gap-1 rounded-xs bg-strip px-3 py-1.5 text-[12px] font-bold tracking-[0.06em] text-ink-3"
            style={{ gridTemplateColumns: template }}
          >
            <span>区分</span>
            <span className="text-right">試合</span>
            {columns.map((name) => (
              <span key={name} className="text-right">
                {name}
              </span>
            ))}
            <span />
          </div>
          <ul className="flex list-none flex-col gap-0.5 p-0">
            {seasons.map((row, at) => (
              <li
                key={row.key}
                // **区切りは面の明るさの差で作る**（基本設計 6.3）。線は引かない
                className={`rounded-xs ${at % 2 === 0 ? 'bg-panel-sub' : 'bg-panel'}`}
              >
                <details>
                  <summary
                    className="grid min-h-13 cursor-pointer list-none items-center gap-1 px-3 py-2"
                    style={{ gridTemplateColumns: template }}
                  >
                    <span className="flex min-w-0 flex-col gap-0.5">
                      <span className="num truncate text-[15px] text-ink-2">{row.label}</span>
                      {row.sublabel !== undefined && (
                        <span className="truncate text-[12px] text-ink-3">{row.sublabel}</span>
                      )}
                    </span>
                    <span className="num text-right text-[15px] text-ink-2">{row.games}</span>
                    {row.cells.map((value, cell) => (
                      <span
                        // 列の見出しと対応する（並びが固定である）
                        key={columns[cell] ?? String(cell)}
                        className="num text-right text-[16px] text-ink"
                      >
                        {num(value)}
                      </span>
                    ))}
                    <span
                      aria-hidden="true"
                      className="disclosure-marker text-right text-[12px] text-ink-3"
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
      )}
    </section>
  );
}
