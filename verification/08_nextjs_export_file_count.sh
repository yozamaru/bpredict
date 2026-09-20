#!/usr/bin/env bash
# P0-14  Next.js 16 の静的書き出しで、1ルートあたり何ファイル生成されるかを測る。
#
# 設計は「1ルートにつき HTML と RSC ペイロードの2ファイル」という前提で、
# 直近5シーズン = 約10,184ファイル（Pages の上限20,000の51%）と見積もっている。
# バージョンによって変わるため、実測して CI の閾値（18,000）が妥当かを確かめる。
#
# 同時に、Turbopack（Next.js 16 の既定）と output: 'export' の組み合わせが
# 成立することも確認する。
#
# ★ リポジトリの中では実行しない。web/ を先に作ってしまうと工程11の実装と混ざる。
#    このスクリプトは必ず mktemp の捨てディレクトリで作業する。

set -euo pipefail

BASE_ROUTES=${BASE_ROUTES:-100}
MORE_ROUTES=${MORE_ROUTES:-300}
WORK=$(mktemp -d)
trap 'echo; echo "作業ディレクトリ: $WORK"; echo "不要なら rm -rf $WORK"' EXIT

echo "=============================================================================="
echo "P0-14  Next.js 静的書き出しの1ルートあたりファイル数"
echo "=============================================================================="
echo "作業ディレクトリ: $WORK  （リポジトリ外）"
echo "Node: $(node --version)"
echo

cd "$WORK"
npx --yes create-next-app@latest app \
  --ts --app --eslint --tailwind --no-src-dir --turbopack \
  --import-alias '@/*' --use-npm --yes >/dev/null 2>&1

cd app
NEXT_VER=$(node -p "require('next/package.json').version")
echo "Next.js: $NEXT_VER"
case "$NEXT_VER" in
  16.*) ;;
  *) echo "★ 警告: 設計は Next.js 16 前提。測定値はそのまま使えない可能性がある" ;;
esac

cat > next.config.ts <<'CFG'
import type { NextConfig } from 'next';
const nextConfig: NextConfig = { output: 'export' };
export default nextConfig;
CFG

# 製品と同じルート形状を作る: /schedule/[date] と /games/[id]
mk_routes () {
  local n=$1
  mkdir -p 'app/schedule/[date]' 'app/games/[id]'
  cat > 'app/schedule/[date]/page.tsx' <<TSX
export function generateStaticParams() {
  return Array.from({ length: $n }, (_, i) => ({ date: \`2026-09-\${String(i).padStart(4, '0')}\` }));
}
export default async function Page({ params }: { params: Promise<{ date: string }> }) {
  const { date } = await params;
  return <main><h1>{date}</h1></main>;
}
TSX
  cat > 'app/games/[id]/page.tsx' <<TSX
export function generateStaticParams() {
  return Array.from({ length: $n }, (_, i) => ({ id: String(100000 + i) }));
}
export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <main><h1>{id}</h1></main>;
}
TSX
}

build_and_count () {
  rm -rf out .next
  npm run build >/dev/null 2>&1 || { echo "★ ビルド失敗。手動で npm run build して原因を見ること"; exit 1; }
  find out -type f | wc -l | tr -d ' '
}

echo
echo "動的ルート ${BASE_ROUTES}本 x 2種 でビルド..."
mk_routes "$BASE_ROUTES"
A=$(build_and_count)
echo "  out/ のファイル数: $A"

echo "動的ルート ${MORE_ROUTES}本 x 2種 でビルド..."
mk_routes "$MORE_ROUTES"
B=$(build_and_count)
echo "  out/ のファイル数: $B"

DELTA_ROUTES=$(( (MORE_ROUTES - BASE_ROUTES) * 2 ))
DELTA_FILES=$(( B - A ))

echo
echo "------------------------------------------------------------------------------"
python3 - "$A" "$B" "$DELTA_ROUTES" "$DELTA_FILES" <<'PY'
import sys
a, b, droutes, dfiles = map(int, sys.argv[1:5])
per = dfiles / droutes
print(f"増えたルート数 : {droutes}")
print(f"増えたファイル数: {dfiles}")
print(f"1ルートあたり  : {per:.2f} ファイル   （設計の前提は 2.00）")
print()
# 設計の見積もり: 直近5シーズン 10,184ファイル = 固定184 + 5,000ルート x 2
FIXED = 184
print(f"{'静的生成の範囲':<22} {'推定ルート数':>12} {'推定ファイル数':>14} {'20,000上限':>11} {'CI閾値18,000':>13}")
print("-" * 78)
for label, routes in (("今季のみ", 1000), ("直近3シーズン", 3000),
                      ("直近5シーズン（採用）", 5000), ("全10シーズン", 10000)):
    total = FIXED + round(routes * per)
    print(f"{label:<22} {routes:>12,} {total:>14,} "
          f"{total / 20000:>10.0%} {'OK' if total <= 18000 else '★超過':>13}")
print()
if per > 2.05:
    print(f"★ 1ルートあたり {per:.2f} ファイルで、前提の 2.00 を超えている。")
    print("  直近5シーズンが 18,000 を超えるなら、静的生成の範囲を3シーズンへ縮小する")
    print("  （docs/design-basic.md 2.5 / requirements 8.2 を先に直してから実装する）")
elif per < 1.95:
    print(f"1ルートあたり {per:.2f} ファイルで前提より少ない。")
    print("  範囲を広げる余地はあるが、広げる判断は別途。閾値18,000は据え置いてよい")
else:
    print("前提どおり 1ルート2ファイル。静的生成の範囲は直近5シーズンのままでよい")
print()
print("verification/RESULTS.md の P0-14 の行に『1ルートあたりのファイル数』を記録すること。")
PY
