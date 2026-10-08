// 「暫定 / 確定 / 序盤」を明示する（要件 8.3、詳細設計 5.3）
//
// **注意状態は常に塗りチップにする**（2026-10-08。スコアボード型。基本設計 6.1）。
// 旧版は暫定を枠線、序盤を `--warn-bg` の淡い地で描き分けていたが、**`--warn-bg` は
// 廃止した** — 白地で 4.5:1 を満たす黄色はもう黄土色で、アウェイの焦橙と 14° しか
// 離れない。**色相ではなく形（塗られた札）で分ける。**
//
// 暫定と序盤はどちらも注意状態であり、同じ塗りを使う。**両者を分けるのは文字である**
// （「暫定」と「序盤」）。色だけで情報を伝えていない（要件 8.6）。
const STYLES = {
  provisional: 'bg-warn text-warn-ink',
  early: 'bg-warn text-warn-ink',
  // 確定は注意状態ではない。構造の帯と同じ無彩の面に本文色を置く
  final: 'bg-strip text-ink-2',
} as const;

const LABELS = {
  provisional: '暫定',
  final: '確定',
  early: '序盤',
} as const;

export function StatusBadge({ kind }: { kind: keyof typeof STYLES }) {
  return (
    <span
      className={`whitespace-nowrap rounded-[2px] px-[7px] py-[3px] text-[12px] font-bold ${STYLES[kind]}`}
    >
      {LABELS[kind]}
    </span>
  );
}
