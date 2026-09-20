import numpy as np
rng = np.random.default_rng(42)

def ece_equal_freq(p, y, n_bins):
    if n_bins < 1: return np.nan
    order = np.argsort(p); p, y = p[order], y[order]
    bins = np.array_split(np.arange(len(p)), n_bins)
    n = len(p)
    return sum(len(b)/n * abs(y[b].mean() - p[b].mean()) for b in bins if len(b) > 0)

print("="*66)
print("V-1  完全較正モデルにおける ECE のノイズフロア")
print("="*66)
print("設定: 予測確率 p ~ Beta(4,3)（平均0.57・バスケの勝率分布に近い）")
print("      y ~ Bernoulli(p) 、予測は p そのもの＝較正は完全")
print("      この条件で出る ECE は 100% サンプリングノイズ")
print()
print(f"{'n':>6} {'ビン数':>6} {'ECE平均':>9} {'ECE 95%点':>10} {'ECE最大':>9}  判定")
print("-"*66)
REP = 4000
rows = []
for n in [200, 300, 500, 800, 1200, 1600, 3000]:
    for mode in ['fixed10', 'adaptive']:
        nb = 10 if mode == 'fixed10' else max(1, min(10, n//50))
        vals = np.empty(REP)
        for i in range(REP):
            p = rng.beta(4, 3, n)
            y = (rng.random(n) < p).astype(float)
            vals[i] = ece_equal_freq(p, y, nb)
        m, p95, mx = vals.mean(), np.percentile(vals, 95), vals.max()
        rows.append((n, mode, nb, m, p95))
        if mode == 'fixed10':
            per_bin = n // nb
            verdict = f"1ビン{per_bin}件" + ("（<50 で不成立）" if per_bin < 50 else "")
            print(f"{n:>6} {nb:>6} {m:>9.4f} {p95:>10.4f} {mx:>9.4f}  {verdict}")
print()
print("【判断材料】設計の採用ゲートは ECE < ノイズフロア。")
for n, mode, nb, m, p95 in rows:
    if mode == 'fixed10' and n in (200, 500, 1200):
        print(f"  n={n:>4}: 完全較正でも ECE は平均 {m:.4f} / 95%点 {p95:.4f} が出る")
print()
print("  → 現設計の『ECE < 0.05』は n=1200 でも 95%点が下回るため")
print("     ほぼ常に通過し、ゲートとして機能していない可能性が高い")
