import { deviation, favoredSide, isTossUp, percentPair, type GameView } from '@/lib/view';

/**
 * 勝率の表示部品（基本設計 6.1 / 6.3。2026-10-08 にスコアボード型を採用した）。
 *
 * **数値が主役である。** 勝率は一覧・詳細ともに 40〜44px で置く（基本設計 6.2）。
 * 旧版は一覧 12〜13px / 詳細 38px で、**この画面の主役である数値が本文と同じ
 * 大きさだった**。主役の立て方はテーマで違う — ライトは「彩度の孤立」（勝率だけが
 * 有彩色で、ボードの他すべてが無彩の灰）、ダークは「輝度」（最明色が自ら光る）。
 * どちらも `--home` / `--away` を数値に当てることで成立する。
 *
 * ホーム／アウェイは**色だけでなく H / A の記号と位置でも示す**（要件 8.6）。
 */

/**
 * ホーム・アウェイの2行（記号・クラブ名・勝率）。**一覧のボードと詳細のヒーローで
 * 同じ組み方を使う**。縮尺だけが違う（基本設計 6.2 の字組み表）。
 *
 * **ここに `aria-hidden` を置かない。** 置くと、呼び出し側が role="img" の
 * ブロックで包むのを忘れたときに**数値が支援技術から消える**（失敗が静かになる）。
 * 包むのは呼び出し側の責任で、詳細設計 5.2 がそう定めている。
 */
export function WinProbRows({ game, size }: { game: GameView; size: 'list' | 'detail' }) {
  const { home, away } = percentPair(game.homeWinProb);
  const detail = size === 'detail';
  const numberSize = detail ? 'text-[44px]' : 'text-[40px]';
  const nameSize = detail ? 'text-[16px]' : 'text-[15px]';

  return (
    <div className="flex flex-col gap-[5px]">
      <WinProbRow
        mark="H"
        side="ホーム"
        tone="text-home"
        name={game.home.name}
        percent={home}
        numberSize={numberSize}
        nameSize={nameSize}
      />
      <WinProbRow
        mark="A"
        side="アウェイ"
        tone="text-away"
        name={game.away.name}
        percent={away}
        numberSize={numberSize}
        nameSize={nameSize}
      />
    </div>
  );
}

function WinProbRow({
  mark,
  side,
  tone,
  name,
  percent,
  numberSize,
  nameSize,
}: {
  mark: 'H' | 'A';
  side: string;
  tone: 'text-home' | 'text-away';
  name: string;
  percent: number;
  numberSize: string;
  nameSize: string;
}) {
  return (
    <div className="flex items-center gap-2">
      {/* 記号は `--panel` の上に置く。**`--strip` の上には置かない**
          （ライトで 4.23:1 となり 4.5 を割る。基本設計 6.1） */}
      <span aria-hidden="true" className={`shrink-0 text-[12px] font-bold tracking-[0.1em] ${tone}`}>
        {mark}
      </span>
      <span className="sr-only">{side}</span>
      {/* クラブ名は `--ink-2`。**`--ink` は主数値と画面見出しだけに予約する** */}
      <span className={`min-w-0 flex-1 truncate font-semibold text-ink-2 ${nameSize}`}>{name}</span>
      <span className={`num shrink-0 leading-none ${numberSize} ${tone}`}>
        {percent}
        <span className="text-[0.4em]">%</span>
      </span>
    </div>
  );
}

/**
 * 詳細画面の勝率ブロック。**2行の勝率とバーをひとまとめにして出す。**
 *
 * ブロック全体に role="img" と一文の aria-label を与え、内部の数値とバーは
 * aria-hidden にする。両方に読み上げ対象があると重複する（詳細設計 5.2）。
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
        <WinProbRows game={game} size="detail" />

        {/* 詳細では幅が取れるので 1ポイント = 全幅の 1%。帯は ±5% */}
        <AxisBar
          homeWinProb={game.homeWinProb}
          className="mt-3 h-[14px] rounded-[1px]"
          bandHalf="5%"
          devScale="1%"
        />

        <div className="mt-1.5 flex justify-between text-[12px] text-ink-3">
          <span>アウェイ100%</span>
          <span className="text-ink-2">50 — 50</span>
          <span>ホーム100%</span>
        </div>
      </div>
    </div>
  );
}

/**
 * バーの本体だけを切り出したもの。一覧（ボードの中）と詳細で**縮尺だけを替えて**使う。
 * **縮尺は呼び出し側が渡す。** 圧縮や強調はしない。
 *
 * **表の列に軸を通す前提（`.axis-column`）は捨てた**（2026-10-08）。あれは
 * `<table>` の列をセルの全高で貫くための仕組みで、スコアボード型では表そのものが
 * 無い。バーはボードの中にあり、**溝（`--groove`）が目盛りの全体を示すため、
 * 軸をバーの外へ伸ばす必要がなくなった。**
 */
export function AxisBar({
  homeWinProb,
  className = '',
  bandHalf,
  devScale,
}: {
  homeWinProb: number;
  className?: string;
  /** 互角帯の半幅。±5ポイントを縮尺に合わせて渡す */
  bandHalf: string;
  /** 1ポイントあたりの幅。**必ず % で渡す** — px だと隔たり45ポイントで器を越える */
  devScale: string;
}) {
  const dev = deviation(homeWinProb);
  const side = favoredSide(homeWinProb);

  return (
    <div
      aria-hidden="true"
      className={`axis-bar ${className}`}
      style={{ '--band-half': bandHalf } as React.CSSProperties}
    >
      {/* 向きが有利な側を示す。色だけに頼らない（要件 8.6） */}
      <span
        className={`axis-fill ${side === 'home' ? 'axis-fill-home' : 'axis-fill-away'}`}
        style={{ '--dev': dev, '--dev-scale': devScale } as React.CSSProperties}
      />
    </div>
  );
}
