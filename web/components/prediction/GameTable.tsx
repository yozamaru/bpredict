import Link from 'next/link';
import { AxisBar } from '@/components/prediction/ProbabilityBar';
import { StatusBadge } from '@/components/prediction/StatusBadge';
import { isTossUp, percentPair, statusBadgeKind, type GameView } from '@/lib/view';

/**
 * その日の全試合を1枚の表に収める（詳細設計 5.3）。
 *
 * **カードを積まない。** 1試合 = 1レコードで、実体は `<tbody>` に `<tr>` 2本
 * （ホーム行 / アウェイ行）。時刻・バー・状態は `rowspan` で束ねる。こうすると
 * 「勝率」列も「予想得点」列も上から下へ一直線に読み下せる一方、スクリーンリーダーは
 * 「架空アルファーズ / 68% / 84」と行単位で読む。
 *
 * `<table>` + `<th scope>` を使うのは、**これが本当に表形式のデータだから**である。
 *
 * 行の高さ 44px は密度の下限である。タップ対象 44×44px の制約からこれ以上は詰められず、
 * 空いた縦を第2行（アウェイ）に使い切っている。
 */
export function GameTable({ games, dateLabel }: { games: GameView[]; dateLabel: string }) {
  return (
    <div className="mt-2 border border-rule bg-panel">
      <table className="w-full table-fixed border-collapse">
        <caption className="sr-only">
          {dateLabel}の試合と予測。1試合につきホーム・アウェイの2行で構成されます。
        </caption>
        <colgroup>
          <col className="w-[46px]" />
          <col />
          <col className="w-[34px]" />
          <col className="w-[72px]" />
          <col className="w-[30px]" />
          <col className="w-[38px]" />
        </colgroup>
        <thead>
          <tr className="[&>th]:border-b-[1.5px] [&>th]:border-ink [&>th]:px-1 [&>th]:pb-[3px] [&>th]:pt-1 [&>th]:text-[9.5px] [&>th]:font-bold [&>th]:tracking-[0.05em] [&>th]:text-ink-2">
            <th scope="col" className="text-left">
              時刻
            </th>
            <th scope="col" className="text-left">
              クラブ
            </th>
            <th scope="col" className="text-right">
              勝率
            </th>
            <th scope="col" className="text-center">
              優勢
              <span aria-hidden="true" className="mt-px flex justify-between font-mono text-[9px]">
                <span>A</span>
                <span>H</span>
              </span>
            </th>
            <th scope="col" className="text-right">
              予想
              <span className="sr-only">得点</span>
            </th>
            <th scope="col" className="text-center">
              状態
            </th>
          </tr>
        </thead>

        {games.map((game) => (
          <GameRows key={game.gameId} game={game} />
        ))}
      </table>
    </div>
  );
}

function GameRows({ game }: { game: GameView }) {
  const { home, away } = percentPair(game.homeWinProb);
  const tossUp = isTossUp(game.homeWinProb);
  const kind = statusBadgeKind(game);
  const label =
    `ホーム ${game.home.name} の勝率${home}パーセント、` +
    `アウェイ ${game.away.name} の勝率${away}パーセント。` +
    (tossUp ? 'ほぼ互角。' : '') +
    `予想スコア ${game.predHomeScore}対${game.predAwayScore}`;

  return (
    <tbody className={`border-t border-rule first-of-type:border-t-0 ${tossUp ? 'bg-tint' : ''}`}>
      <tr>
        {/* 時刻セルが 44×44px のタップ対象で、試合詳細への導線になる */}
        <th
          scope="rowgroup"
          rowSpan={2}
          className="border-r border-rule-soft text-left align-middle"
        >
          <Link
            href={`/games/${game.gameId}/`}
            className="flex h-full min-h-11 min-w-11 flex-col items-start justify-center px-1"
            aria-label={`${game.home.name} 対 ${game.away.name} の試合詳細`}
          >
            <span className="font-mono text-[12px] font-semibold -tracking-[0.02em] text-ink">
              {game.tipoffLabel ?? '未定'}
            </span>
            <span className="mt-px text-[9.5px] text-ink-3">詳細›</span>
          </Link>
        </th>

        <ClubCell side="home" name={game.home.name} />
        <PercentCell value={home} leading />
        {/* 軸はセルの全高に通す（`axis-column`）。バーの内側だけだと、塗りの隣に
            互角帯の片側が見えるだけになり「2色の積み上げバー」に読める（詳細設計 5.3） */}
        <td rowSpan={2} className="axis-column border-x border-rule-soft align-middle">
          <span role="img" aria-label={label} className="flex h-11 flex-col items-center justify-center gap-[3px]">
            {/* **縮尺は % にする。** 1ポイント = 1px では、確率がクランプの上限（95%）に
                寄ったとき隔たり45ポイント = 45px となり、半幅 31px を越えて隣の列へ溢れる。
                % ならバーの半分が 50ポイントに対応し、構造的に収まる */}
            <AxisBar
              homeWinProb={game.homeWinProb}
              className="h-3 w-full"
              bandHalf="5%"
              devScale="1%"
            />
            {/* 色だけでなく文字でも示す（要件 8.3 / 8.6） */}
            <span aria-hidden="true" className="h-[11px] text-[9px] font-bold leading-[11px] tracking-[0.06em] text-ink-2">
              {tossUp ? '互角' : ''}
            </span>
          </span>
        </td>
        <ScoreCell value={game.predHomeScore} leading />
        <td rowSpan={2} className="border-l border-rule-soft text-center align-middle">
          {game.isEarlySeason ? (
            <StatusBadge kind="early" />
          ) : kind ? (
            <StatusBadge kind={kind} />
          ) : (
            <span className="font-mono text-[11px] text-ink-3" aria-label="出場選手は発表済み">
              —
            </span>
          )}
        </td>
      </tr>
      <tr>
        <ClubCell side="away" name={game.away.name} />
        <PercentCell value={away} />
        <ScoreCell value={game.predAwayScore} />
      </tr>
    </tbody>
  );
}

/** ホーム／アウェイは色だけでなく H / A のラベルでも示す（要件 8.6） */
function ClubCell({ side, name }: { side: 'home' | 'away'; name: string }) {
  return (
    <td className="overflow-hidden">
      <span className="flex h-[22px] min-w-0 items-center gap-1 pl-[5px] pr-0.5">
        <span
          aria-hidden="true"
          className={`flex size-3 shrink-0 items-center justify-center font-mono text-[8px] font-bold leading-none text-panel ${
            side === 'home' ? 'bg-home' : 'bg-away'
          }`}
        >
          {side === 'home' ? 'H' : 'A'}
        </span>
        <span
          className={`min-w-0 flex-1 truncate text-[12px] leading-tight ${
            side === 'home' ? 'text-ink' : 'text-ink-2'
          }`}
        >
          {name}
        </span>
        <span className="sr-only">（{side === 'home' ? 'ホーム' : 'アウェイ'}）</span>
      </span>
    </td>
  );
}

/** 勝率は必ず両チーム分を出す。バーが片側にしか出ないため数値は2行とも出す */
function PercentCell({ value, leading = false }: { value: number; leading?: boolean }) {
  return (
    <td className="text-right">
      <span
        className={`block h-[22px] pr-1 font-mono text-[12.5px] leading-[22px] -tracking-[0.04em] ${
          leading ? 'font-semibold text-ink' : 'text-ink-2'
        }`}
      >
        {value}%
      </span>
    </td>
  );
}

/** 予想スコアは整数（要件 8.3）。平均誤差 8〜10点に対し小数第1位は精度の誤認を招く */
function ScoreCell({ value, leading = false }: { value: number; leading?: boolean }) {
  return (
    <td className="text-right">
      <span
        className={`block h-[22px] pr-[5px] font-mono text-[13px] leading-[22px] ${
          leading ? 'font-semibold text-ink' : 'text-ink-2'
        }`}
      >
        {value}
      </span>
    </td>
  );
}
