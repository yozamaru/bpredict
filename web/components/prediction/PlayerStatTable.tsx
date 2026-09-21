import { derive, PCT_THRESHOLD, type PlayerView } from '@/lib/view';

/** 率は**必ず分数と併記する**。単独で `48.9%` と出さない（要件 8.3） */
function Shooting({ made, attempted, pct }: { made: number; attempted: number; pct: number | null }) {
  return (
    <span>
      {made.toFixed(1)} / {attempted.toFixed(1)}
      {pct !== null && <span className="text-text-2"> （{(pct * 100).toFixed(1)}%）</span>}
    </span>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-2 py-0.5">
      <dt className="text-[12px] text-text-2">{label}</dt>
      <dd className="text-[13px]">{children}</dd>
    </div>
  );
}

/**
 * 段階開示（要件 8.3）。初期表示は MIN / PTS / TR / AS の4項目だけで、
 * 展開でフルボックススコアを出す。20項目 × 最大10人を一度に出すと読めない。
 * 展開状態は localStorage に保存しない（毎回たたんだ状態で開く。詳細設計 5.3）。
 */
export function PlayerStatTable({ players }: { players: PlayerView[] }) {
  // P(出場) < 0.5 の選手は表示しない（要件 6.8.4）
  const shown = players.filter((p) => p.availProb >= 0.5);

  return (
    <section className="mt-6">
      <h3 className="text-[15px] font-extrabold">個人スタッツ予測</h3>
      <p className="mt-1 text-[11px] leading-relaxed text-text-3">
        出場確率50%以上の選手を表示しています。± は直近の試合における誤差の目安です。
      </p>
      <ul className="mt-2 flex flex-col gap-1.5">
        {shown.map((player) => {
          const d = derive(player);
          return (
            <li key={player.playerId} className="rounded-xl border border-border bg-surface">
              <details>
                <summary className="flex min-h-11 cursor-pointer list-none items-center gap-2 px-3 py-2">
                  <span className="w-24 shrink-0 truncate text-[13px] font-bold">{player.name}</span>
                  <span className="w-7 shrink-0 text-[11px] text-text-2">{player.position}</span>
                  <span className="flex-1 text-right text-[13px]">{player.minutes.toFixed(1)}分</span>
                  <span className="w-11 text-right text-[13px] font-bold">{d.pts.toFixed(1)}</span>
                  <span className="w-9 text-right text-[13px]">{d.reb.toFixed(1)}</span>
                  <span className="w-9 text-right text-[13px]">{player.ast.toFixed(1)}</span>
                  <span aria-hidden="true" className="w-3 text-right text-[11px] text-text-2">
                    ▾
                  </span>
                </summary>
                <dl className="border-t border-border px-3 py-2">
                  <Row label="出場時間">
                    {player.minutes.toFixed(1)}分 <span className="text-text-2">±{player.err.minutes.toFixed(1)}</span>
                  </Row>
                  <Row label="得点">
                    {d.pts.toFixed(1)} <span className="text-text-2">±{player.err.pts.toFixed(1)}</span>
                  </Row>
                  <Row label="FG">
                    <Shooting made={d.fgm} attempted={d.fga} pct={d.fgPct} />
                  </Row>
                  <Row label="　2P">
                    <Shooting
                      made={d.fg2m}
                      attempted={player.fg2a}
                      pct={player.fg2a >= PCT_THRESHOLD.split ? player.fg2Pct : null}
                    />
                  </Row>
                  <Row label="　3P">
                    <Shooting
                      made={d.fg3m}
                      attempted={player.fg3a}
                      pct={player.fg3a >= PCT_THRESHOLD.split ? player.fg3Pct : null}
                    />
                  </Row>
                  <Row label="FT">
                    <Shooting
                      made={d.ftm}
                      attempted={player.fta}
                      pct={player.fta >= PCT_THRESHOLD.ft ? player.ftPct : null}
                    />
                  </Row>
                  <Row label="リバウンド">
                    {d.reb.toFixed(1)} <span className="text-text-2">±{player.err.reb.toFixed(1)}</span>
                    <span className="text-text-2">
                      {' '}
                      （OR {player.oreb.toFixed(1)} / DR {player.dreb.toFixed(1)}）
                    </span>
                  </Row>
                  <Row label="アシスト">
                    {player.ast.toFixed(1)} <span className="text-text-2">±{player.err.ast.toFixed(1)}</span>
                  </Row>
                  <Row label="ターンオーバー">{player.tov.toFixed(1)}</Row>
                  <Row label="スティール">{player.stl.toFixed(1)}</Row>
                  <Row label="ブロック">{player.blk.toFixed(1)}</Row>
                  <Row label="ファウル">{player.pf.toFixed(1)}</Row>
                  <Row label="被ファウル">{player.fd.toFixed(1)}</Row>
                  {d.efgPct !== null && (
                    <Row label="EFG%">{(d.efgPct * 100).toFixed(1)}%</Row>
                  )}
                </dl>
              </details>
            </li>
          );
        })}
      </ul>
      {/* ST / BS は MAE が平均値と同水準になる。隠さずに書く（要件 6.8.6） */}
      <p className="mt-2 text-[11px] leading-relaxed text-text-3">
        スティールとブロックは1試合あたりの回数が少なく、予測はその選手の平均に近い値になります。
      </p>
    </section>
  );
}
