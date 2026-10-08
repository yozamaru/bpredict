import type { ActualView } from '@/lib/view';

/**
 * その試合の記録（詳細設計 3.3 / 5.3 の v1.130）。
 *
 * **`PlayerStatTable`（予測）と別の表にする**（運営者の判断。2026-10-09）。
 * 母集団が違うため1つの表に混ぜられない — 予測は `P(出場) >= 0.5` の選手、実績は
 * **実際に出場した全員**である。混ぜると、
 *
 * - 予測が無いのに出場した選手の行で予測列が全部空になる（**外したのではなく、
 *   候補に入っていなかった**）
 * - **予測が1本も無い試合では行が1つも作れない** — これが決定的である
 *
 * **予測の有無に依存しない。** 呼び出し側は `prediction` が null でもこの表を出す。
 */

/** 1行の列割り。**`PlayerStatTable` と同じ**（並べたときに桁が揃う）。 */
const COLS = 'grid grid-cols-[minmax(0,1fr)_28px_46px_46px_32px_32px] items-center gap-x-1.5';

/** 記録が無い項目は「—」。**0 と書かない**（0 は「0回」を意味する）。 */
function N({ value, digits = 0 }: { value: number | null; digits?: number }) {
  if (value === null) return <span className="text-ink-3">—</span>;
  return <>{value.toFixed(digits)}</>;
}

/**
 * 実績の率は**必ず分数と併記する**（要件 8.3）。
 *
 * **閾値を設けない。** 予測側は試投数が閾値未満なら率を出さないが、実績の `1 / 2` は
 * 丸めのない事実である。**試投数が0のときだけ率を出さない**（サーバが null にする）。
 */
function Shooting({ value }: { value: { m: number | null; a: number | null; pct: number | null } }) {
  if (value.m === null || value.a === null) return <span className="text-ink-3">—</span>;
  return (
    <span className="num">
      {value.m} / {value.a}
      {value.pct !== null && (
        <span className="text-ink-3"> （{(value.pct * 100).toFixed(1)}%）</span>
      )}
    </span>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-[3px]">
      <dt className="shrink-0 text-[12px] text-ink-3">{label}</dt>
      <dd className="num text-right text-[15px] text-ink">{children}</dd>
    </div>
  );
}

/**
 * クラブごとの合計。**出ている値を足した数を出す**（`PlayerStatTable` と同じ作法）。
 *
 * **記録が無い選手は足さない。** NULL を 0 として足すと、欠けた試合が黙って
 * 無視される（詳細設計 1.9 が `plus_minus` を集計しない理由と同じ）。
 */
function totalsOf(rows: ActualView[]): { minutes: number; pts: number } {
  return rows.reduce(
    (acc, r) => ({
      minutes: acc.minutes + (r.minutes ?? 0),
      pts: acc.pts + (r.pts ?? 0),
    }),
    { minutes: 0, pts: 0 },
  );
}

export function PlayerActualTable({
  actuals,
  clubs,
}: {
  actuals: ActualView[];
  /** ホーム・アウェイの順。**合計はクラブ別に出す** */
  clubs: { clubId: string; label: string }[];
}) {
  // **閾値で絞らない。** 行があること自体が「出場した」という事実である
  const groups = clubs.map((club) => ({
    ...club,
    members: actuals.filter((r) => r.clubId === club.clubId),
  }));
  const known = new Set(clubs.map((c) => c.clubId));
  const others = actuals.filter((r) => !known.has(r.clubId));
  if (others.length > 0) groups.push({ clubId: '', label: 'その他', members: others });

  return (
    <section className="mt-6">
      <h3 className="font-serif text-[16px] font-semibold text-ink-2">この試合の記録</h3>
      <p className="mt-1 text-[12px] leading-relaxed text-ink-3">
        出場した選手の公式記録です。行を開くとフルボックススコアを出します。
      </p>

      <div
        aria-hidden="true"
        className={`${COLS} mt-2.5 px-3.5 pb-1 text-[12px] font-bold tracking-[0.04em] text-ink-3`}
      >
        <span>選手</span>
        <span>位置</span>
        <span className="text-right">分</span>
        <span className="text-right">得点</span>
        <span className="text-right">R</span>
        <span className="text-right">A</span>
      </div>

      {groups.map((group) => (
        <div key={group.clubId || 'other'} className="mt-3">
          <h4 className="px-3.5 pb-1 text-[12px] font-bold tracking-[0.04em] text-ink-3">
            {group.label}
          </h4>
          <ul className="flex flex-col gap-0.5">
            {group.members.map((row) => (
              <li key={row.playerId}>
                <details className="rounded-[2px] bg-panel open:bg-panel-sub">
                  <summary className={`${COLS} min-h-12 cursor-pointer list-none px-3.5`}>
                    <span className="flex min-w-0 items-center gap-1.5">
                      <span className="truncate text-[15px] font-semibold text-ink-2">
                        {row.name}
                      </span>
                      {/* スターターを印で示す。**色だけに頼らない**（要件 8.6） */}
                      {row.started === true && (
                        <span className="shrink-0 text-[12px] leading-none text-ink-3">
                          <span aria-hidden="true">●</span>
                          <span className="sr-only">スターター</span>
                        </span>
                      )}
                      <span
                        aria-hidden="true"
                        className="disclosure-marker shrink-0 text-[12px] leading-none text-ink-3"
                      >
                        ▾
                      </span>
                    </span>
                    <span className="text-[12px] text-ink-3">{row.position ?? '—'}</span>
                    <span className="num text-right text-[17px] text-ink">
                      <N value={row.minutes} digits={1} />
                      <span className="sr-only">分</span>
                    </span>
                    <span className="num text-right text-[17px] text-ink">
                      <N value={row.pts} />
                      <span className="sr-only">点</span>
                    </span>
                    <span className="num text-right text-[15px] text-ink-2">
                      <N value={row.reb} />
                      <span className="sr-only">リバウンド</span>
                    </span>
                    <span className="num text-right text-[15px] text-ink-2">
                      <N value={row.ast} />
                      <span className="sr-only">アシスト</span>
                    </span>
                  </summary>
                  <dl className="px-3.5 pb-3.5">
                    <Row label="出場時間">
                      <N value={row.minutes} digits={1} />分
                    </Row>
                    <Row label="得点">
                      <N value={row.pts} />
                    </Row>
                    <Row label="FG">
                      <Shooting value={row.fg} />
                    </Row>
                    <Row label="　2P">
                      <Shooting value={row.fg2} />
                    </Row>
                    <Row label="　3P">
                      <Shooting value={row.fg3} />
                    </Row>
                    <Row label="FT">
                      <Shooting value={row.ft} />
                    </Row>
                    <Row label="リバウンド">
                      <N value={row.reb} />
                      <span className="text-ink-3">
                        {' '}
                        （OR <N value={row.oreb} /> / DR <N value={row.dreb} />）
                      </span>
                    </Row>
                    <Row label="アシスト">
                      <N value={row.ast} />
                    </Row>
                    <Row label="ターンオーバー">
                      <N value={row.tov} />
                    </Row>
                    <Row label="スティール">
                      <N value={row.stl} />
                    </Row>
                    <Row label="ブロック">
                      <N value={row.blk} />
                    </Row>
                    <Row label="ファウル">
                      <N value={row.pf} />
                    </Row>
                    <Row label="被ファウル">
                      <N value={row.fd} />
                    </Row>
                    {/* **＋/－ は実績のみ**（要件 6.8.3）。予測しない指標である */}
                    <Row label="＋/－">
                      {row.plusMinus === null ? (
                        <span className="text-ink-3">—</span>
                      ) : (
                        <>{row.plusMinus > 0 ? `＋${row.plusMinus}` : row.plusMinus}</>
                      )}
                    </Row>
                    {row.efgPct !== null && (
                      <Row label="EFG%">{(row.efgPct * 100).toFixed(1)}%</Row>
                    )}
                  </dl>
                </details>
              </li>
            ))}
          </ul>
          {group.members.length > 0 && (
            <dl
              className={`${COLS} mt-0.5 min-h-11 items-center rounded-[2px] bg-strip px-3.5`}
            >
              <dt className="col-span-2 text-[12px] font-bold text-ink-2">合計</dt>
              <dd className="num text-right text-[15px] text-ink">
                {totalsOf(group.members).minutes.toFixed(1)}
                <span className="sr-only">分</span>
              </dd>
              <dd className="num text-right text-[15px] text-ink">
                {totalsOf(group.members).pts}
                <span className="sr-only">点</span>
              </dd>
              <dd aria-hidden="true" />
              <dd aria-hidden="true" />
            </dl>
          )}
        </div>
      ))}

      {/* 固定注記。**省略しない**（要件 6.9 / R-11）。
          **予測側の注記を使い回さない** — あちらは「統計的推定値」で、ここは実績である
          （詳細設計 5.3 の `StatNotes` と同じ理由） */}
      <p className="mt-2.5 text-[12px] leading-relaxed text-ink-3">
        本サイトが公式記録から集計した実績値です。選手の能力や評価を示すものではありません。
      </p>
    </section>
  );
}
