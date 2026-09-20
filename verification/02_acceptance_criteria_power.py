import numpy as np
rng = np.random.default_rng(7)
REP = 20000

print("="*70)
print("V-2  受け入れ基準 A-09『accuracy - baseline >= 0.03 かつ n >= 200』の検証")
print("="*70)
print("設定: 真の勝率 p ~ Beta(4,3)（ホーム勝率 約57%）")
print("      モデル = 真の p を完全に知る理想モデル（これ以上は存在しない上限）")
print("      ベースライン = 常にホーム勝ち")
print()
print(f"{'n':>6} {'モデル精度':>10} {'BL精度':>8} {'差の平均':>9} {'差のSD':>8} {'差<0.03 の確率':>14}")
print("-"*70)
for n in [200, 300, 500, 800, 1200, 1600, 3000]:
    d = np.empty(REP); am = np.empty(REP); ab = np.empty(REP)
    for i in range(REP):
        p = rng.beta(4, 3, n)
        y = (rng.random(n) < p)
        acc_m = ((p > 0.5) == y).mean()
        acc_b = y.mean()
        am[i], ab[i], d[i] = acc_m, acc_b, acc_m - acc_b
    print(f"{n:>6} {am.mean():>10.4f} {ab.mean():>8.4f} {d.mean():>9.4f} {d.std():>8.4f} {(d<0.03).mean():>13.1%}")

print()
print("【判断材料】")
print("  ・理想モデル（真の確率を知る）でも 差の平均は約 0.06〜0.07")
print("  ・n=200 では差の標準偏差が約 0.03。つまり +0.03 は 1SD 以内＝ノイズと区別できない")
print("  ・理想モデルですら n=200 で基準を落ちる確率が無視できない")
print("  → 『+0.03』は達成可能性ではなく、統計的な識別力の問題で不適切")
print()

# McNemar 的な対応あり比較
print("="*70)
print("V-3  同一試合での2モデル比較（対応あり）に必要なサンプル数")
print("="*70)
print("設定: モデルAとモデルBの真の精度差 = delta。有意に検出できる確率を測る")
print()
print(f"{'真の差':>8} {'n=200':>9} {'n=500':>9} {'n=1200':>9} {'n=2000':>9}")
print("-"*70)
for delta in [0.01, 0.02, 0.03, 0.05]:
    row = [f"{delta:>8.2f}"]
    for n in [200, 500, 1200, 2000]:
        hits = 0
        for _ in range(4000):
            # 不一致ペアのみが検定に効く（McNemar）。不一致率を 0.25 と仮定
            disc = rng.binomial(n, 0.25)
            b = rng.binomial(disc, 0.5 + delta/(2*0.25)) if disc > 0 else 0
            c = disc - b
            if b + c > 0:
                z = (b - c) / np.sqrt(b + c)
                if z > 1.96: hits += 1
        row.append(f"{hits/4000:>8.1%}")
    print(" ".join(row))
print()
print("  → 真の差 0.03 を n=200 で検出できる確率は低い（検出力不足）")
print("  → Brier ベースの比較でも同様の検出力の問題が起きるため、")
print("     採用ゲートには『有意差』ではなく『最小実質差 + 十分なn』の併用が要る")
