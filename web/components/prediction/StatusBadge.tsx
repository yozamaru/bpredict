// 「暫定 / 確定 / 序盤」を明示する（要件 8.3、詳細設計 5.3）
const STYLES = {
  provisional: 'border border-text-3 text-text-2',
  final: 'bg-accent-bg text-accent',
  early: 'bg-warn-bg text-warn',
} as const;

const LABELS = {
  provisional: '暫定',
  final: '確定',
  early: '序盤',
} as const;

export function StatusBadge({ kind }: { kind: keyof typeof STYLES }) {
  return (
    <span className={`rounded-md px-1.5 py-0.5 text-[10px] font-extrabold ${STYLES[kind]}`}>
      {LABELS[kind]}
    </span>
  );
}
