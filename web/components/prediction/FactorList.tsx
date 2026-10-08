import type { FactorView, GameView } from '@/lib/view';

/**
 * この予測に使った項目（詳細設計 2.7.2 / 5.3 の v1.132）。
 *
 * 運営者の指摘は「**なぜこの予測になったかが2項目しかないので、どの項目から
 * この予測を導き出したのかを詳しく知りたい**」である。
 *
 * **`ReasonList`（根拠）とは別の問いに答える。**
 *
 * | 根拠（`ReasonList`） | 使った項目（この部品） |
 * |---|---|
 * | **なぜそうなったか。** 寄与を要因グループに集約する | **何を見たか。** 列をそのまま並べる |
 * | 有利な側（`favors`）と強さを出す | **有利不利を主張しない。** 値が大きい側だけ |
 * | 現在の21列では**2件**しか出ない | **21件すべて**出る |
 *
 * **寄与の大きい順に並べない**（サーバが `rank` で固定している）。並べ替えると
 * 「どれがどれだけ効いたか」を主張することになり、この部品が避けている話に戻る。
 *
 * **初期はたたむ。** 21項目を常に出すと、根拠2件が読まれなくなる（段階開示。要件 8.3）。
 */

/** 要因グループの表示名（詳細設計 2.7 の表）。生の `group_key` を画面に出さない。 */
const GROUP_LABEL: Record<string, string> = {
  TEAM_STRENGTH: 'チーム力',
  SCHEDULE: '日程・疲労',
  PLAYER: '選手',
  VENUE: '会場',
};

export function FactorList({
  factors,
  view,
}: {
  factors: FactorView[];
  view: GameView;
}) {
  // **サーバの並びを保つ。** グループの切れ目だけを見出しにする
  const groups: { key: string; members: FactorView[] }[] = [];
  for (const factor of factors) {
    const last = groups[groups.length - 1];
    if (last !== undefined && last.key === factor.group) last.members.push(factor);
    else groups.push({ key: factor.group, members: [factor] });
  }

  return (
    <section className="mt-3">
      <details className="rounded-[2px] bg-panel">
        <summary className="flex min-h-12 cursor-pointer list-none items-center justify-between gap-2 px-3.5">
          <span className="font-serif text-[16px] font-semibold text-ink-2">
            この予測に使った項目
          </span>
          <span className="flex shrink-0 items-center gap-1.5">
            <span className="num text-[12px] text-ink-3">{factors.length}件</span>
            <span
              aria-hidden="true"
              className="disclosure-marker text-[12px] leading-none text-ink-3"
            >
              ▾
            </span>
          </span>
        </summary>

        <div className="px-3.5 pb-3.5">
          {/* **「どれがどれだけ効いたか」は出せないと書く**（要件 8.3「予測の
              確からしさを隠さない」）。出せない理由は、寄与を項目ごとに出すと
              相関する列の係数が互いに打ち消し合い、1列の符号を「その特徴量の効果」
              として読ませてしまうことである（詳細設計 2.7） */}
          <p className="text-[12px] leading-relaxed text-ink-3">
            モデルが見た数値をそのまま並べています。
            <b className="font-bold text-ink-2">どちらが有利かはここでは示しません</b>
            （項目ごとの効き方は「なぜこの予測になったか」が要因グループ単位で示します）。
          </p>

          {groups.map((group) => (
            <div key={group.key} className="mt-3">
              <h4 className="pb-1 text-[12px] font-bold tracking-[0.04em] text-ink-3">
                {GROUP_LABEL[group.key] ?? group.key}
              </h4>
              <dl className="flex flex-col">
                {group.members.map((factor) => (
                  <div
                    key={factor.label}
                    className="flex items-baseline justify-between gap-3 border-b border-rule py-1.5 last:border-b-0"
                  >
                    <dt className="shrink-0 text-[13px] text-ink-2">{factor.label}</dt>
                    <dd className="num text-right text-[14px] text-ink">
                      {factor.value}
                      {/* **「ホームが大きい」と書く。** 「ホーム有利」ではない —
                          係数が負の列（守備効率）では両者が逆を向く（2.7.2） */}
                      {factor.larger !== null && (
                        <span className="ml-1.5 text-[12px] text-ink-3">
                          {factor.larger === 'HOME' ? view.home.shortName : view.away.shortName}
                          が大きい
                        </span>
                      )}
                    </dd>
                  </div>
                ))}
              </dl>
            </div>
          ))}
        </div>
      </details>
    </section>
  );
}
