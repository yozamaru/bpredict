// 「暫定 / 確定 / 序盤」を明示する（要件 8.3、詳細設計 5.3）
//
// **暫定と序盤は同じ --warn を使うが、塗りと枠線で区別する。** 色だけで情報を
// 伝えないため（要件 8.6）、塗られているか否かという形の差を持たせている。
const STYLES = {
  provisional: 'border border-warn text-warn',
  final: 'border border-rule bg-tint text-ink-2',
  early: 'border border-warn-bg bg-warn-bg text-warn',
} as const;

const LABELS = {
  provisional: '暫定',
  final: '確定',
  early: '序盤',
} as const;

export function StatusBadge({ kind }: { kind: keyof typeof STYLES }) {
  return (
    <span className={`whitespace-nowrap px-1 py-0.5 text-[10px] font-extrabold -tracking-[0.04em] ${STYLES[kind]}`}>
      {LABELS[kind]}
    </span>
  );
}
