'use client';

// 試合詳細（基本設計 5.2 / 詳細設計 3.3）。
//
// **当日の試合は静的JSON、それ以外は公開API**（基本設計 2.5 の表）。
// **レスポンスの形は試合前後で同一である**（詳細設計 3.3）— 変わるのは中身であり、
// キーの位置ではない。したがって1本の経路で両方を描ける。
//
// 2026-10-08 に「スコアボード型」へ移した（要件 8.1 / 基本設計 6.1〜6.3）。
// **1試合 = 1枚のボード**にし、区切りを罫線から**面の明るさの差**に変えた。

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { PlayerStatTable } from '@/components/prediction/PlayerStatTable';
import { ProbabilityBar } from '@/components/prediction/ProbabilityBar';
import { ReasonList } from '@/components/prediction/ReasonList';
import { ResultComparison } from '@/components/prediction/ResultComparison';
import { StatusBadge } from '@/components/prediction/StatusBadge';
import { EmptyState } from '@/components/ui/EmptyState';
import { ACTIONS, LOADING, LOAD_ERROR, NO_PREDICTION } from '@/lib/messages';
import { toClub, toGame, toPlayers, toReason } from '@/lib/map';
import { fetchGameDetail, fetchGameFromApi, type GameDetail } from '@/lib/source';
import {
  favoredSide,
  isTossUp,
  statusBadgeKind,
  type GameView,
  type ReasonView,
} from '@/lib/view';

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
        className="mt-5 rounded-[2px] bg-panel-sub px-3.5 py-4 text-center text-[15px] text-ink-3"
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
  // **導出はサーバが済ませている。** 画面は写すだけ（ui-implementation スキル）
  const players = toPlayers(detail.playerPredictions);

  return (
    <>
      {/* 画面の見出しだけは最上段（--ink）に置く。数値と競合しない（基本設計 6.1） */}
      <h2 className="mt-5 font-serif text-[19px] font-semibold leading-snug tracking-[0.02em] text-ink">
        <Link href={`/teams/${home.slug}/`} className="underline decoration-rule">
          {home.name}
        </Link>{' '}
        <span className="text-ink-3">対</span>{' '}
        <Link href={`/teams/${away.slug}/`} className="underline decoration-rule">
          {away.name}
        </Link>
      </h2>
      <p className="mt-1 text-[12px] text-ink-3">{game.venue?.name ?? '会場は未発表'}</p>

      {view === null ? (
        // **「予測はまだない」を黙って空にしない**（要件 8.5）
        <div className="mt-4">
          <EmptyState message={NO_PREDICTION} />
        </div>
      ) : (
        <>
          <Scoreboard view={view} />
          {detail.evaluation !== null && <Finished detail={detail} view={view} />}
          {reasons.length > 0 && <Reasons reasons={reasons} view={view} />}
          {players.length > 0 && (
            <PlayerStatTable
              players={players}
              /* **ホーム・アウェイの順。** 合計はクラブ別に出す（詳細設計 5.3） */
              clubs={[
                { clubId: home.clubId, label: home.name },
                { clubId: away.clubId, label: away.name },
              ]}
            />
          )}
          <Notes detail={detail} players={players.length} />
        </>
      )}
    </>
  );
}

/**
 * ヒーローのスコアボード。**勝率と予想スコアを1枚の面に載せる。**
 *
 * **予想スコアはこの版で初めて画面に出る。** 旧版は `ProbabilityBar` の
 * `aria-label` にしか入っておらず、**目で見える場所に1つも無かった**（要件 F-03 は
 * 両チームの予想得点を整数で表示することを求めている）。
 *
 * **勝率そのものは `ProbabilityBar` が描く**（一覧と共有する部品であり、
 * 基本設計 6.2 は「一覧と詳細で勝率の大きさを変えない」と定めている）。
 */
function Scoreboard({ view }: { view: GameView }) {
  // **出す状態が無いときはバッジを出さない。** `statusBadgeKind` は該当なしで
  // null を返す（暫定でも確定でも序盤でもない状態が存在する）
  const badge = statusBadgeKind(view);
  const tossUp = isTossUp(view.homeWinProb);

  return (
    <div className="mt-3.5 rounded-[2px] bg-panel px-3.5 pb-4 pt-3">
      <div className="flex items-center justify-between gap-2">
        {/* 時刻はラベルの段（12px。基本設計 6.2）。ただし数値なので等幅で組む */}
        <span className="num text-[12px] tracking-[0.06em] text-ink-3">
          {view.tipoffLabel ?? '未定'}
        </span>
        <span className="flex items-center gap-2">
          {/* 優劣は色だけでなく**文字でも**示す（要件 8.3 / 8.6）。互角は塗りの札に
              して、一方的な試合と**形でも**区別する（一覧のボードと同じ組み方）。

              **`aria-hidden` にしない。** `ProbabilityBar` の `aria-label` は
              互角のときだけ「ほぼ互角」と言うため、優勢の向きは読み上げに残らない */}
          {tossUp ? (
            <span className="rounded-[2px] bg-strip px-1.5 py-0.5 text-[12px] font-bold text-ink-2">
              ほぼ互角
            </span>
          ) : (
            <span className="text-[12px] text-ink-3">
              {favoredSide(view.homeWinProb) === 'home' ? 'ホーム優勢' : 'アウェイ優勢'}
            </span>
          )}
          {/* **序盤と暫定は両立する**（一覧のボードと同じ。要件 8.4 は3状態すべてを
              明示することを求めており、旧版の詳細は序盤を1度も出していなかった） */}
          {view.isEarlySeason && <StatusBadge kind="early" />}
          {badge !== null && <StatusBadge kind={badge} />}
        </span>
      </div>

      <div className="mt-2.5">
        <ProbabilityBar game={view} />
      </div>

      {/* 装飾の仕切り。意味を持つ線は 50%軸だけである（基本設計 6.3） */}
      <div className="mt-3 h-px bg-rule" />

      <div className="mt-2.5 flex items-end justify-between gap-2">
        <span className="shrink-0 text-[12px] font-bold tracking-[0.08em] text-ink-3">
          予想スコア
        </span>
        {/* 整数で出す（要件 8.3）。平均誤差 8〜10点に対し小数第1位は精度の誤認を招く。
            どちらの得点かは `sr-only` で補う（画面では H / A の並びが示している） */}
        <span className="num text-[34px] leading-none text-ink">
          <span className="sr-only">ホーム </span>
          {view.predHomeScore} <span className="text-ink-3">–</span>
          <span className="sr-only"> アウェイ </span> {view.predAwayScore}
        </span>
      </div>
    </div>
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
    <div className="mt-2.5">
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

/**
 * 出せていないものを隠さない（要件 8.3「予測の確からしさを隠さない」）。
 *
 * **従属情報の面（`--panel-sub`）に載せる。** 主役ではないが、隠すものでもない
 * （基本設計 6.1）。
 */
function Notes({ detail, players }: { detail: GameDetail; players: number }) {
  return (
    <ul className="mt-6 list-none space-y-1.5 rounded-[2px] bg-panel-sub px-3.5 py-3 text-[12px] leading-relaxed text-ink-3">
      <li className="relative pl-[13px] before:absolute before:left-0 before:text-ink-3 before:content-['—']">
        予想スコアは<b className="font-bold text-ink-2">整数</b>で表示します。
        1試合あたりの平均誤差が8〜10点あるため、小数第1位は精度の誤認を招きます。
      </li>
      {players === 0 && (
        // **理由を断定しない。** 出ない原因は複数ある（30本が未登録 /
        // チーム目標が到達不能で破棄 / 季の1試合目で候補が空）。
        // 画面で切り分けられないものを断定して書かない
        <li className="relative pl-[13px] before:absolute before:left-0 before:text-ink-3 before:content-['—']">
          <b className="font-bold text-ink-2">この試合の個人スタッツ予測はありません。</b>
        </li>
      )}
      {detail.prediction !== null && detail.prediction.isProvisional && (
        <li className="relative pl-[13px] before:absolute before:left-0 before:text-ink-3 before:content-['—']">
          出場選手が未発表のため<b className="font-bold text-ink-2">暫定</b>です。
        </li>
      )}
    </ul>
  );
}
