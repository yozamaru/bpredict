import Link from 'next/link';
import { GameTable } from '@/components/prediction/GameTable';
import { StaleBanner } from '@/components/prediction/StaleBanner';
import {
  SAMPLE_ACCURACY,
  SAMPLE_DATE_LABEL,
  SAMPLE_DAY,
  SAMPLE_GAMES,
  SAMPLE_STALE,
} from '@/lib/fixtures/today';
import { byTipoff, isTossUp } from '@/lib/view';
import { ACTIONS, noGames } from '@/lib/messages';
import { EmptyState } from '@/components/ui/EmptyState';

// 静的出力。ISR は使わない（要件 7章）
export const dynamic = 'force-static';

export default function Page() {
  // **開始時刻の昇順に並べる。** 一覧は日程であり、時刻順でないと読めない。
  // 並べ替えを画面側に持たせるのは、静的JSON の並びに依存しないようにするため
  const games = [...SAMPLE_GAMES].sort(byTipoff);
  const tossUps = games.filter((game) => isTossUp(game.homeWinProb));

  return (
    <>
      {SAMPLE_STALE.stale && <StaleBanner generatedAtLabel={SAMPLE_STALE.generatedAtLabel} />}

      {/* 工程11a の間だけ出す。実データへ結線する 11b で外す */}
      <p className="mt-3 border-l-[3px] border-warn bg-warn-bg px-2 py-1 text-[10.5px] leading-snug text-warn">
        これは表示を確認するための合成データです。実際の予測ではなく、クラブ名も架空です。
      </p>

      {/* 的中率には母数を併記し、**ベースラインを同じ大きさで隣に置く**（要件 8.3）。
          自分の成績だけを見せない */}
      <dl className="mt-2.5 grid grid-cols-2 border border-rule bg-panel">
        <div className="min-w-0 px-2 py-1.5">
          <dt className="whitespace-nowrap text-[9.5px] tracking-[0.06em] text-ink-3">今季的中率</dt>
          <dd className="mt-px font-mono text-[17px] font-semibold leading-tight text-ink">
            {(SAMPLE_ACCURACY.rate * 100).toFixed(1)}%
            <span className="mt-px block font-sans text-[9.5px] font-normal text-ink-3">
              {SAMPLE_ACCURACY.n}試合
            </span>
          </dd>
        </div>
        <div className="min-w-0 border-l border-rule-soft px-2 py-1.5">
          <dt className="whitespace-nowrap text-[9.5px] tracking-[0.06em] text-ink-3">ベースライン</dt>
          <dd className="mt-px font-mono text-[15px] font-semibold leading-tight text-ink-2">
            {(SAMPLE_ACCURACY.baselineRate * 100).toFixed(1)}%
            <span className="mt-px block font-sans text-[9.5px] font-normal text-ink-3">
              ホーム必勝・{SAMPLE_ACCURACY.n}試合
            </span>
          </dd>
        </div>
      </dl>

      <div className="mt-4 flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <h2 className="font-serif text-[19px] font-semibold tracking-[0.02em]">
          {SAMPLE_DATE_LABEL}の予測
        </h2>
        <span className="font-mono text-[11px] text-ink-2">全{games.length}試合</span>
      </div>

      {/* **日付別へ辿れるようにする**（基本設計 5.1 の `/ ─→ /schedule/[date]`）。
          導線がないと、URL を手で打つ以外に他の日を見る方法がない */}
      <nav aria-label="日付の移動" className="mt-2.5 flex items-stretch gap-2">
        {SAMPLE_DAY.previous && (
          <Link
            href={`/schedule/${SAMPLE_DAY.previous}/`}
            className="flex min-h-11 flex-1 items-center justify-center border border-rule text-[13px] font-bold"
          >
            ← 前日
          </Link>
        )}
        {SAMPLE_DAY.next && (
          <Link
            href={`/schedule/${SAMPLE_DAY.next}/`}
            className="flex min-h-11 flex-1 items-center justify-center border border-rule text-[13px] font-bold"
          >
            {SAMPLE_DAY.nextLabel} →
          </Link>
        )}
      </nav>

      {games.length > 0 ? (
        <>
          {/* **しきい値を画面に描く。** 互角かどうかの判定を読者が自分の目で検算できる
              ようにするため、凡例は畳まず常設する（詳細設計 5.3） */}
          <p className="mt-3 border border-dashed border-rule px-[7px] py-[5px] text-[10px] leading-relaxed text-ink-3">
            <b className="font-bold text-ink-2">H</b>＝ホーム／
            <b className="font-bold text-ink-2">A</b>＝アウェイ。バーは勝率の
            <b className="font-bold text-ink-2">50%からの隔たり</b>
            を優勢な側へ伸ばしたもの（1ポイント＝1px）。
            <span
              aria-hidden="true"
              className="mx-0.5 inline-block h-[9px] w-4 border-x border-axis bg-band align-[-1px]"
            />
            の網かけ帯（±5ポイント）に収まっていれば
            <b className="font-bold text-ink-2">ほぼ互角</b>で、
            バーが短い試合ほどモデルが読めていないことを示します。
          </p>

          <GameTable games={games} dateLabel={SAMPLE_DATE_LABEL} />

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
          </ul>
        </>
      ) : (
        <div className="mt-3">
          <EmptyState message={noGames(SAMPLE_DATE_LABEL)} action={ACTIONS.accuracy} />
        </div>
      )}
    </>
  );
}
