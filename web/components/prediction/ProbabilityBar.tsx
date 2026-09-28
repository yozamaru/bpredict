import { deviation, favoredSide, isTossUp, percentPair, type GameView } from '@/lib/view';

/**
 * 中央基準（ダイバージング）バー。**50% を軸に、勝率が 50 からどれだけ離れているかだけを
 * 優勢な側へ伸ばす**（詳細設計 5.3）。
 *
 * 左詰めのバーは全行が左端から始まるため、どの行も似た見た目になり、**68% と 52% の差
 * （このツールが最も伝えるべき差）がひと目で立ち上がらない**。中央基準にすると、拮抗した
 * 試合は「軸の上にほとんど何も無い」という視覚的欠落として現れる。
 *
 * 45〜55% にあたる ±5ポイントの帯を同じ位置に常設するので、**互角かどうかの判定を読者が
 * 自分の目で検算できる**。しきい値を隠して「ほぼ互角」とだけ書かない。
 *
 * 勝率は**必ず両チーム分**を表示する（要件 8.3）。ブロック全体に role="img" と一文の
 * aria-label を与え、内部の数値とバーは aria-hidden にする。両方に読み上げ対象があると
 * 重複する（詳細設計 5.2）。
 */
export function ProbabilityBar({ game }: { game: GameView }) {
  const { home, away } = percentPair(game.homeWinProb);
  const tossUp = isTossUp(game.homeWinProb);
  const label =
    `ホーム ${game.home.name} の勝率${home}パーセント、` +
    `アウェイ ${game.away.name} の勝率${away}パーセント。` +
    (tossUp ? 'ほぼ互角。' : '') +
    `予想スコア ${game.predHomeScore}対${game.predAwayScore}`;

  return (
    <div role="img" aria-label={label}>
      <div aria-hidden="true">
        <div className="flex items-end justify-between gap-2">
          <div className="min-w-0">
            <p className="text-[9.5px] font-bold tracking-[0.14em] text-ink-3">ホーム</p>
            <p className="truncate text-[12.5px] leading-tight text-ink-2">{game.home.name}</p>
            <p className="font-mono text-[38px] font-semibold leading-none tracking-tight text-home">
              {home}
              <span className="ml-0.5 text-[0.45em]">%</span>
            </p>
          </div>
          <div className="min-w-0 text-right">
            <p className="text-[9.5px] font-bold tracking-[0.14em] text-ink-3">アウェイ</p>
            <p className="truncate text-[12.5px] leading-tight text-ink-2">{game.away.name}</p>
            <p className="font-mono text-[38px] font-semibold leading-none tracking-tight text-away">
              {away}
              <span className="ml-0.5 text-[0.45em]">%</span>
            </p>
          </div>
        </div>

        {/* 詳細では幅が取れるので 1ポイント = 全幅の 1%。帯は ±5% */}
        <AxisBar
          homeWinProb={game.homeWinProb}
          className="mt-3.5 h-[18px] border-y border-rule-soft"
          bandHalf="5%"
          devScale="1%"
          axisOverhang="-4px"
        />

        <div className="mt-px flex justify-between font-mono text-[9px] font-bold text-ink-3">
          <span>アウェイ100%</span>
          <span className="text-ink-2">50 — 50</span>
          <span>ホーム100%</span>
        </div>
      </div>
    </div>
  );
}

/**
 * バーの本体だけを切り出したもの。一覧（等倍・1ポイント = 1px）と詳細（1ポイント = 全幅の1%）で
 * 縮尺だけを替えて使う。**縮尺は呼び出し側が渡す。** 圧縮や強調はしない。
 */
export function AxisBar({
  homeWinProb,
  className = '',
  bandHalf,
  devScale,
  axisOverhang = '0px',
}: {
  homeWinProb: number;
  className?: string;
  /** 互角帯の半幅。±5ポイントを縮尺に合わせて渡す */
  bandHalf: string;
  /** 1ポイントあたりの幅 */
  devScale: string;
  /** 50%軸を上下へはみ出させる量 */
  axisOverhang?: string;
}) {
  const dev = deviation(homeWinProb);
  const side = favoredSide(homeWinProb);

  return (
    <div
      aria-hidden="true"
      className={`axis-bar ${className}`}
      style={
        {
          '--band-half': bandHalf,
          '--axis-overhang': axisOverhang,
        } as React.CSSProperties
      }
    >
      {/* 向きが有利な側を示す。色だけに頼らない（要件 8.6） */}
      <span
        className={`axis-fill ${side === 'home' ? 'axis-fill-home' : 'axis-fill-away'}`}
        style={{ '--dev': dev, '--dev-scale': devScale } as React.CSSProperties}
      />
    </div>
  );
}
