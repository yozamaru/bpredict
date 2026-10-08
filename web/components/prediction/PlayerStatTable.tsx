import type { PlayerView } from '@/lib/view';

/**
 * 1行の列割り。**見出しと各行で同じものを使う**（別に書くと桁がずれる）。
 * 375px（ボード幅 343px − 左右 28px = 315px）に収まるよう固定幅を詰めてある。
 */
const COLS = 'grid grid-cols-[minmax(0,1fr)_28px_46px_46px_32px_32px] items-center gap-x-1.5';

/**
 * 率は**必ず分数と併記する**。単独で `48.9%` と出さない（要件 8.3）。
 * `pct` が null なら試投数が閾値未満で、率を出さず分数だけにする。
 * 閾値の判定はサーバ側で済んでいる（詳細設計 3.3）。
 */
function Shooting({ value }: { value: { m: number; a: number; pct: number | null } }) {
  return (
    <span className="num">
      {value.m.toFixed(1)} / {value.a.toFixed(1)}
      {value.pct !== null && (
        <span className="text-ink-3"> （{(value.pct * 100).toFixed(1)}%）</span>
      )}
    </span>
  );
}

/**
 * 誤差の目安。**null なら何も出さない**（要件 6.8.6 の `N` が未定義であり、
 * 過去の個人予測と実績の対比が1件もない。詳細設計 2.3.1）。
 * **0 と書かない** — 0 は「誤差がない」という意味を持ってしまう。
 */
function Error_({ value }: { value: number | null }) {
  if (value === null) return null;
  return <span className="text-ink-3"> ±{value.toFixed(1)}</span>;
}

/**
 * 展開したフルボックススコアは**縦に積む**（基本設計 6.3）。
 * 375px に20列は入らないため、横に並べる案は採らない。
 */
function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-[3px]">
      <dt className="shrink-0 text-[12px] text-ink-3">{label}</dt>
      <dd className="num text-right text-[15px] text-ink">{children}</dd>
    </div>
  );
}

/**
 * 段階開示（要件 8.3）。初期表示は MIN / PTS / TR / AS の4項目だけで、
 * 展開でフルボックススコアを出す。20項目 × 最大10人を一度に出すと読めない。
 * 展開状態は localStorage に保存しない（毎回たたんだ状態で開く。詳細設計 5.3）。
 *
 * **展開した行は面が半歩うしろへ下がる**（2026-10-08。スコアボード型）。
 * フルボックススコアは主役ではない従属情報であり、`--panel-sub` がその面である
 * （基本設計 6.1）。開閉は CSS だけで行う（`globals.css` の `details`）。
 */
/**
 * クラブごとの合計（詳細設計 5.3）。
 *
 * **画面に出ている値を足した数を出す。** 整合化そのものは予想スコアに厳密一致するが、
 * 各選手を小数第1位に丸めると列の和がずれることがある。丸める前の値を出すと
 * **読者が列を足した結果と合計行が食い違う**ため、同じ画面に矛盾した数字を並べない
 * 方を採る（詳細設計 5.3）。
 */
function totalsOf(players: PlayerView[]): { minutes: number; pts: number } {
  return players.reduce(
    (acc, p) => ({
      minutes: acc.minutes + Number(p.minutes.toFixed(1)),
      pts: acc.pts + Number(p.derived.pts.toFixed(1)),
    }),
    { minutes: 0, pts: 0 },
  );
}

export function PlayerStatTable({
  players,
  clubs,
}: {
  players: PlayerView[];
  /** ホーム・アウェイの順。**合計はクラブ別に出す**（両チームを足すと 400分になる） */
  clubs: { clubId: string; label: string }[];
}) {
  // P(出場) < 0.5 の選手は表示しない（要件 6.8.4）
  const shown = players.filter((p) => p.availProb >= 0.5);

  /**
   * **クラブ別に畳む。** `clubs` に無いクラブの選手は最後にまとめる — 落とすと
   * 画面から選手が静かに消える（API が想定外の `clubId` を返したときに気づけない）。
   */
  const groups = clubs.map((club) => ({
    ...club,
    members: shown.filter((p) => p.clubId === club.clubId),
  }));
  const known = new Set(clubs.map((c) => c.clubId));
  const others = shown.filter((p) => !known.has(p.clubId));
  if (others.length > 0) groups.push({ clubId: '', label: 'その他', members: others });

  return (
    <section className="mt-6">
      <h3 className="font-serif text-[16px] font-semibold text-ink-2">個人スタッツ予測</h3>
      <p className="mt-1 text-[12px] leading-relaxed text-ink-3">
        出場確率50%以上の選手を表示しています。行を開くとフルボックススコアを出します。
      </p>

      {/* 列の見出し。**略記には読み上げ用の全称を添える**（要件 8.6） */}
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
          {/* **クラブ名を出す。** どちらのチームの合計なのかが分からないと、
              合計行が予想スコアと一致していることを確かめられない */}
          <h4 className="px-3.5 pb-1 text-[12px] font-bold tracking-[0.04em] text-ink-3">
            {group.label}
          </h4>
          <ul className="flex flex-col gap-0.5">
            {group.members.map((player) => {
          // 導出はサーバが済ませている。画面では計算しない（ui-implementation スキル）
          const d = player.derived;
          return (
            <li key={player.playerId}>
              <details className="rounded-[2px] bg-panel open:bg-panel-sub">
                <summary className={`${COLS} min-h-12 cursor-pointer list-none px-3.5`}>
                  <span className="flex min-w-0 items-center gap-1.5">
                    <span className="truncate text-[15px] font-semibold text-ink-2">
                      {player.name}
                    </span>
                    <span
                      aria-hidden="true"
                      className="disclosure-marker shrink-0 text-[12px] leading-none text-ink-3"
                    >
                      ▾
                    </span>
                  </span>
                  {/* 未登録は「—」。**空にしない** — 列が詰まって見える */}
                  <span className="text-[12px] text-ink-3">{player.position ?? '—'}</span>
                  <span className="num text-right text-[17px] text-ink">
                    {player.minutes.toFixed(1)}
                    <span className="sr-only">分</span>
                  </span>
                  <span className="num text-right text-[17px] text-ink">
                    {d.pts.toFixed(1)}
                    <span className="sr-only">点</span>
                  </span>
                  <span className="num text-right text-[15px] text-ink-2">
                    {d.reb.toFixed(1)}
                    <span className="sr-only">リバウンド</span>
                  </span>
                  <span className="num text-right text-[15px] text-ink-2">
                    {player.ast.toFixed(1)}
                    <span className="sr-only">アシスト</span>
                  </span>
                </summary>
                <dl className="px-3.5 pb-3.5">
                  <Row label="出場時間">
                    {player.minutes.toFixed(1)}分
                    <Error_ value={player.err.minutes} />
                  </Row>
                  <Row label="得点">
                    {d.pts.toFixed(1)}
                    <Error_ value={player.err.pts} />
                  </Row>
                  <Row label="FG">
                    <Shooting value={d.fg} />
                  </Row>
                  <Row label="　2P">
                    <Shooting value={d.fg2} />
                  </Row>
                  <Row label="　3P">
                    <Shooting value={d.fg3} />
                  </Row>
                  <Row label="FT">
                    <Shooting value={d.ft} />
                  </Row>
                  <Row label="リバウンド">
                    {d.reb.toFixed(1)}
                    <Error_ value={player.err.reb} />
                    <span className="text-ink-3">
                      {' '}
                      （OR {player.oreb.toFixed(1)} / DR {player.dreb.toFixed(1)}）
                    </span>
                  </Row>
                  <Row label="アシスト">
                    {player.ast.toFixed(1)}
                    <Error_ value={player.err.ast} />
                  </Row>
                  <Row label="ターンオーバー">{player.tov.toFixed(1)}</Row>
                  <Row label="スティール">{player.stl.toFixed(1)}</Row>
                  <Row label="ブロック">{player.blk.toFixed(1)}</Row>
                  <Row label="ファウル">{player.pf.toFixed(1)}</Row>
                  <Row label="被ファウル">{player.fd.toFixed(1)}</Row>
                  {d.efgPct !== null && <Row label="EFG%">{(d.efgPct * 100).toFixed(1)}%</Row>}
                </dl>
              </details>
            </li>
          );
        })}
          </ul>
          {/* 合計行（詳細設計 5.3）。**構造の帯**なので `--strip` に置く。
              総出場時間 200.0分 ＝ 5人 × 40分 で、得点はチームの予想スコアに一致する */}
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
                {totalsOf(group.members).pts.toFixed(1)}
                <span className="sr-only">点</span>
              </dd>
              <dd aria-hidden="true" />
              <dd aria-hidden="true" />
            </dl>
          )}
        </div>
      ))}

      {/* ST / BS は MAE が平均値と同水準になる。隠さずに書く（要件 6.8.6） */}
      <p className="mt-2.5 text-[12px] leading-relaxed text-ink-3">
        スティールとブロックは1試合あたりの回数が少なく、予測はその選手の平均に近い値になります。
      </p>
      {/* 成功率も試投数が少なく、**学習しないベースラインに負ける項目である**
          （`fg3_pct` −0.15% / `ft_pct` −1.98%。詳細設計 2.3.1 の実測）。
          **ST / BS と同じ扱いにする** — 隠さず1行で書く（運営者の判断。2026-10-05） */}
      <p className="mt-1 text-[12px] leading-relaxed text-ink-3">
        1試合の成功率は試投数が少なく、誤差が大きい項目です。
      </p>
      {/* 固定注記。**省略しない**（ui-implementation スキル / 要件 4.5.4） */}
      <p className="mt-1 text-[12px] leading-relaxed text-ink-3">
        個人予測は過去の公式記録から算出した統計的推定値であり、選手の能力や評価を示すものではありません。
      </p>
    </section>
  );
}
