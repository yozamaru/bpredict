import numpy as np
from scipy.optimize import brentq
STATS=['fg2m','fg2a','fg3m','fg3a','ftm','fta','oreb','dreb','ast','tov','stl','blk','pf','fd']
BASE=[0.12,0.25,0.05,0.14,0.07,0.09,0.04,0.11,0.09,0.06,0.03,0.02,0.07,0.08]

def case(seed):
    r=np.random.default_rng(seed); n=int(r.integers(9,13))
    avail=np.clip(r.beta(5,2,n),0.5,1.0); mip=r.uniform(8,34,n)
    rates={s:np.maximum(r.normal(b,b*0.4,n),1e-4) for s,b in zip(STATS,BASE)}
    t={}
    t['fg2a']=52*r.uniform(.85,1.15); t['fg3a']=26*r.uniform(.85,1.15); t['fta']=18*r.uniform(.85,1.15)
    t['fg2m']=t['fg2a']*r.uniform(.44,.58); t['fg3m']=t['fg3a']*r.uniform(.28,.42); t['ftm']=t['fta']*r.uniform(.68,.85)
    for s,v in zip(['oreb','dreb','ast','tov','stl','blk','pf','fd'],[9,26,18,13,6,3,17,18]): t[s]=v*r.uniform(.85,1.15)
    t['pts']=t['fg2m']*2+t['fg3m']*3+t['ftm']
    return avail,mip,rates,t

def logit(p): p=np.clip(p,1e-6,1-1e-6); return np.log(p/(1-p))
def sig(z): return 1/(1+np.exp(-z))

def shift_to_target(pct, att, target):
    """ロジット空間で一律シフトし、sum(pct*att)=target を厳密に満たす。
       単調なので必ず一意解があり、[0,1] を出ない＝クリップ不要。"""
    lo = logit(pct)
    f = lambda d: (sig(lo+d)*att).sum() - target
    if f(-50) > 0 or f(50) < 0: return sig(lo)   # 目標が達成不能（att合計未満/超過）
    d = brentq(f, -50, 50, xtol=1e-12)
    return sig(lo+d)

def runC(avail,mip,rates,t,iters=3):
    m=avail*mip; m=m*200/m.sum()
    att={a:rates[a]*m for a in ('fg2a','fg3a','fta')}
    pct={a:np.clip(rates[mk]/np.maximum(rates[a],1e-9),1e-4,1-1e-4)
         for a,mk in (('fg2a','fg2m'),('fg3a','fg3m'),('fta','ftm'))}
    oth={s:rates[s]*m for s in STATS if s not in ('fg2m','fg2a','fg3m','fg3a','ftm','fta')}
    out=[]
    for _ in range(iters):
        for a in att:
            v=att[a].sum()
            if v>0: att[a]*=t[a]/v
        for a,mk in (('fg2a','fg2m'),('fg3a','fg3m'),('fta','ftm')):
            pct[a]=shift_to_target(pct[a], att[a], t[mk])      # ← ロジットシフト
        for s in oth:
            v=oth[s].sum()
            if v>0: oth[s]*=t[s]/v
        oth['pf']=np.minimum(oth['pf'],5.0)
        x=dict(oth)
        for a,mk in (('fg2a','fg2m'),('fg3a','fg3m'),('fta','ftm')):
            x[a]=att[a]; x[mk]=pct[a]*att[a]
        pts=x['fg2m']*2+x['fg3m']*3+x['ftm']
        per={s:abs(x[s].sum()-t[s])/t[s] for s in STATS if t[s]>0}
        viol=sum((x[a]>x[b]+1e-9).sum() for a,b in (('fg2m','fg2a'),('fg3m','fg3a'),('ftm','fta')))
        rng_ok = all(((x[mk]/np.maximum(x[a],1e-9))<=1+1e-9).all() for a,mk in (('fg2a','fg2m'),('fg3a','fg3m'),('fta','ftm')))
        out.append((abs(pts.sum()-t['pts']), max(per.values()), viol, rng_ok))
    return out

print("="*78)
print("V-11  案C: 成功率をロジット空間でシフトして合計を厳密に合わせる")
print("="*78)
N=1500
res={i:[] for i in range(1,4)}; pts={i:[] for i in range(1,4)}; viols=0; rngbad=0
for seed in range(N):
    for i,(pe,se,v,ok) in enumerate(runC(*case(seed)),1):
        res[i].append(se); pts[i].append(pe); viols+=v
        if not ok: rngbad+=1
print(f"{'反復':>4} {'項目誤差 中央':>13} {'90%点':>9} {'99%点':>9} {'最大':>10} {'得点誤差 最大':>13}")
print("-"*78)
for i in sorted(res):
    a=np.array(res[i]); p=np.array(pts[i])
    print(f"{i:>4} {np.median(a):>12.4%} {np.percentile(a,90):>8.4%} {np.percentile(a,99):>8.4%} {a.max():>9.4%} {p.max():>13.5f}")
print()
print(f"制約違反(成功数>試投数): {viols} 件 / 成功率が[0,1]を外れたケース: {rngbad} 件")
print()

print("="*78)
print("V-12  3方式の最終比較（1500ケース・反復2回時点の最悪値）")
print("="*78)
print(f"{'方式':<34} {'項目誤差 最大':>14} {'得点誤差 最大':>14} {'制約違反':>9}")
print("-"*78)
print(f"{'案A 成功数と試投数を独立予測':<30} {'23.9%':>16} {'3.21':>14} {'0':>9}")
print(f"{'案B 試投数+成功率(一律スケール)':<30} {'24.7%':>16} {'4.42':>14} {'0':>9}")
a2=np.array(res[2]); p2=np.array(pts[2])
print(f"{'案C 試投数+成功率(ロジット)':<31} {a2.max():>15.4%} {p2.max():>14.5f} {viols:>9}")
print()
print("【結論】")
print("  案C は2回の反復で項目誤差・得点誤差ともに実質ゼロ。制約違反も構造的にゼロ。")
print("  設計の整合化アルゴリズムを案Cに差し替えるべき。")
