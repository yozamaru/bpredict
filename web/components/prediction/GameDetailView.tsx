'use client';

// 試合詳細（基本設計 5.2 / 詳細設計 3.3）。
//
// **当日の試合は静的JSON、それ以外は公開API**（基本設計 2.5 の表）。
// **レスポンスの形は試合前後で同一である**（詳細設計 3.3）— 変わるのは中身であり、
// キーの位置ではない。したがって1本の経路で両方を描ける。

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { ProbabilityBar } from '@/components/prediction/ProbabilityBar';
import { ReasonList } from '@/components/prediction/ReasonList';
import { ResultComparison } from '@/components/prediction/ResultComparison';
import { StatusBadge } from '@/components/prediction/StatusBadge';
import { EmptyState } from '@/components/ui/EmptyState';
import { ACTIONS, LOADING, LOAD_ERROR, NO_PREDICTION } from '@/lib/messages';
import { toClub, toGame, toReason } from '@/lib/map';
import { fetchGameDetail, fetchGameFromApi, type GameDetail } from '@/lib/source';
import { statusBadgeKind, type GameView, type ReasonView } from '@/lib/view';

export function GameDetailView({ gameId }: { gameId: string }) {
  const [detail, setDetail] = useState<GameDetail | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    // **静的JSON は当日の試合だけである**（詳細設計 3.7）。無ければ API へ落ちる
    fetchGameDetail(gameId)
      .catch(() => fetchGameFromApi(gameId))
      .then((found) => {
        if (alive) setDetail(found);
      })
      .catch(() => {
        if (alive) setFailed(true);
      });
    return () => {
      alive = false;
    };
  }, [gameId]);

  if (failed) return <EmptyState message={LOAD_ERROR} action={ACTIONS.about} />;
  if (detail === null) {
    return (
      <p
        aria-live="polite"
        className="mt-5 border border-dashed border-rule px-2 py-3 text-center text-[12.5px] text-ink-3"
      >
        {LOADING}
      </p>
    );
  }

  const { game } = detail;
  const home = toClub(game.home);
  const away = toClub(game.away);
  const view = toGame({ ...game, prediction: detail.prediction });
  const reasons: ReasonView[] = (detail.prediction?.reasons ?? []).map(toReason);
  // **出す状態が無いときはバッジを出さない。** `statusBadgeKind` は該当なしで
  // null を返す（暫定でも確定でも序盤でもない状態が存在する）
  const badge = view === null ? null : statusBadgeKind(view);

  return (
    <>
      <h2 className="mt-5 font-serif text-[19px] font-semibold tracking-[0.02em]">
        <Link href={`/teams/${home.slug}/`} className="underline decoration-rule">
          {home.name}
        </Link>{' '}
        <span className="text-ink-2">対</span>{' '}
        <Link href={`/teams/${away.slug}/`} className="underline decoration-rule">
          {away.name}
        </Link>
      </h2>
      <p className="mt-1 font-mono text-[11.5px] text-ink-2">
        {game.venue?.name ?? '会場は未発表'}
        {badge !== null && (
          <span className="ml-2 align-middle font-sans">
            <StatusBadge kind={badge} />
          </span>
        )}
      </p>

      {view === null ? (
        // **「予測はまだない」を黙って空にしない**（要件 8.5）
        <div className="mt-4">
          <EmptyState message={NO_PREDICTION} />
        </div>
      ) : (
        <>
          <div className="mt-4">
            <ProbabilityBar game={view} />
          </div>
          {detail.evaluation !== null && <Finished detail={detail} view={view} />}
          {reasons.length > 0 && <Reasons reasons={reasons} view={view} />}
          <Notes detail={detail} />
        </>
      )}
    </>
  );
}

/** 試合後の対比。**外れた試合を隠さない**（要件 8.3 / 詳細設計 5.3）。 */
function Finished({ detail, view }: { detail: GameDetail; view: GameView }) {
  const { game, evaluation } = detail;
  // **`VOID`（中止・延期）には対比を出さない。** 的中率の母数から外れる予測であり、
  // 「実績と予測の対比」として出す対象がない（詳細設計 3.3）
  if (
    evaluation === null ||
    evaluation.outcome === 'VOID' ||
    evaluation.isCorrect === null ||
    evaluation.scoreError === null ||
    evaluation.bucketContext === null ||
    game.homeScore === null ||
    game.awayScore === null
  ) {
    return null;
  }
  return (
    <div className="mt-5">
      <ResultComparison
        result={{
          gameId: game.gameId,
          home: { name: view.home.name },
          away: { name: view.away.name },
          homeScore: game.homeScore,
          awayScore: game.awayScore,
          homeWinProb: view.homeWinProb,
          predHomeScore: view.predHomeScore,
          predAwayScore: view.predAwayScore,
          isCorrect: evaluation.isCorrect,
          scoreError: evaluation.scoreError,
          bucket: {
            label: evaluation.bucketContext.bucket,
            n: evaluation.bucketContext.n,
            correct: evaluation.bucketContext.correct,
            rate: evaluation.bucketContext.rate,
          },
        }}
      />
    </div>
  );
}

/**
 * 根拠（詳細設計 2.7）。
 *
 * **要約文を画面で組み立てない。** 公開APIの応答に `summary` がまだ無く
 * （契約ファイルに入っていない）、ここで作ると**設計に無い文言**を画面が持つ。
 * 代わりに、何を並べているかだけを述べる。
 */
function Reasons({ reasons, view }: { reasons: ReasonView[]; view: GameView }) {
  const favored = reasons[0]?.favors === 'AWAY' ? view.away.name : view.home.name;
  return (
    <ReasonList
      summary={`寄与の大きい順に${reasons.length}件を示します。最も大きいのは${favored}に有利な要因です。`}
      reasons={reasons}
    />
  );
}

/** 出せていないものを隠さない（要件 8.3「予測の確からしさを隠さない」）。 */
function Notes({ detail }: { detail: GameDetail }) {
  return (
    <ul className="mt-5 list-none space-y-1 p-0 text-[10.5px] leading-relaxed text-ink-3">
      <li className="relative pl-[11px] before:absolute before:left-0 before:text-ink-3 before:content-['—']">
        予想スコアは<b className="font-bold text-ink-2">整数</b>で表示します。
        1試合あたりの平均誤差が8〜10点あるため、小数第1位は精度の誤認を招きます。
      </li>
      {detail.playerPredictions.length === 0 && (
        <li className="relative pl-[11px] before:absolute before:left-0 before:text-ink-3 before:content-['—']">
          <b className="font-bold text-ink-2">個人スタッツの予測はまだ出していません。</b>
          出場選手の発表を取り込む仕組みが未実装のためです。
        </li>
      )}
      {detail.prediction !== null && detail.prediction.isProvisional && (
        <li className="relative pl-[11px] before:absolute before:left-0 before:text-ink-3 before:content-['—']">
          出場選手が未発表のため<b className="font-bold text-ink-2">暫定</b>です。
        </li>
      )}
    </ul>
  );
}
