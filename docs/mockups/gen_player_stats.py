# -*- coding: utf-8 -*-
"""方向性A・試合詳細の個人スタッツ（合成データ）を生成する。

    python3 docs/mockups/gen_player_stats.py


数値は詳細設計 2.4 の整合化（試投数は動かさず、成功率をロジット空間で
一律シフト）をそのまま通し、選手の得点合計が予想スコアに一致することを
保証する。モックでも矛盾した数字を並べない。
"""
import math

def logit(p): return math.log(p / (1 - p))
def sig(z):   return 1 / (1 + math.exp(-z))

def shift(pcts, atts, target_made):
    """sum(pct_i * att_i) = target_made を満たす共通シフト量を二分法で解く。"""
    lo = [logit(min(max(p, 1e-4), 1 - 1e-4)) for p in pcts]
    f = lambda d: sum(sig(l + d) * a for l, a in zip(lo, atts)) - target_made
    a, b = -50.0, 50.0
    for _ in range(200):
        m = (a + b) / 2
        if f(a) * f(m) <= 0: b = m
        else: a = m
    d = (a + b) / 2
    return [sig(l + d) for l in lo]

def build(players, target_pts, target_min=200.0):
    # 1-2. 期待出場時間を正規化
    tot = sum(p["min"] for p in players)
    for p in players:
        p["min"] = p["min"] * target_min / tot

    # 4'. 試投数をペース側で先に合わせる。
    #     成功率だけで得点を合わせようとすると、試投数の置き方次第で成功率が
    #     非現実的な水準まで押し下げられる（チームFT% 69.5% など）。実際の
    #     パイプラインでも試投数はペース予測から来るので、先にそちらを合わせ、
    #     残差だけを成功率のシフトに渡す。
    pre = sum(p["fg2p"] * p["fg2a"] * 2 + p["fg3p"] * p["fg3a"] * 3 + p["ftp"] * p["fta"]
              for p in players)
    k = target_pts / pre
    for p in players:
        p["fg2a"] *= k; p["fg3a"] *= k; p["fta"] *= k

    # 5. 成功率をチーム目標へロジット空間シフト（得点の恒等式に対して1回）
    #    pts(d) = 2*s(l2+d)*A2 + 3*s(l3+d)*A3 + 1*s(lf+d)*Af
    A2 = [p["fg2a"] for p in players]
    A3 = [p["fg3a"] for p in players]
    Af = [p["fta"]  for p in players]
    l2 = [logit(p["fg2p"]) for p in players]
    l3 = [logit(p["fg3p"]) for p in players]
    lf = [logit(p["ftp"])  for p in players]

    def pts(d):
        return (sum(2 * sig(l + d) * a for l, a in zip(l2, A2))
              + sum(3 * sig(l + d) * a for l, a in zip(l3, A3))
              + sum(1 * sig(l + d) * a for l, a in zip(lf, Af)))

    a, b = -50.0, 50.0
    for _ in range(200):
        m = (a + b) / 2
        if (pts(a) - target_pts) * (pts(m) - target_pts) <= 0: b = m
        else: a = m
    d = (a + b) / 2
    for p, x2, x3, xf in zip(players, l2, l3, lf):
        p["fg2p"], p["fg3p"], p["ftp"] = sig(x2 + d), sig(x3 + d), sig(xf + d)

    # 6. 成功数・得点は導出（独立に持たない）
    for p in players:
        p["fg2m"] = p["fg2p"] * p["fg2a"]
        p["fg3m"] = p["fg3p"] * p["fg3a"]
        p["ftm"]  = p["ftp"]  * p["fta"]
        p["fgm"]  = p["fg2m"] + p["fg3m"]
        p["fga"]  = p["fg2a"] + p["fg3a"]
        p["pts"]  = p["fg2m"] * 2 + p["fg3m"] * 3 + p["ftm"]
        p["reb"]  = p["oreb"] + p["dreb"]
        p["fgp"]  = p["fgm"] / p["fga"] if p["fga"] else None
        p["efg"]  = (p["fgm"] + 0.5 * p["fg3m"]) / p["fga"] if p["fga"] else None
        den = 2 * (p["fga"] + 0.44 * p["fta"])
        p["ts"]   = p["pts"] / den if den else None
    return players

# ── 架空アルファーズ（ホーム・予想84点） ───────────────────────────
home = [
 dict(no="0",  nm="架空 太郎", pos="PG", av=.96, min=31.2, fg2a=7.8, fg3a=5.3, fta=3.9,
      fg2p=.526, fg3p=.434, ftp=.846, oreb=0.6, dreb=2.5, ast=6.1, tov=2.2, stl=1.1, blk=0.3, pf=2.4, fd=3.1,
      e_min=5.8, e_pts=4.8, e_reb=2.1, e_ast=1.5),
 dict(no="7",  nm="架空 次郎", pos="SG", av=.93, min=28.4, fg2a=5.1, fg3a=6.8, fta=2.4,
      fg2p=.505, fg3p=.381, ftp=.812, oreb=0.5, dreb=2.8, ast=2.4, tov=1.6, stl=0.9, blk=0.2, pf=2.1, fd=2.0,
      e_min=5.2, e_pts=4.5, e_reb=1.8, e_ast=1.2),
 dict(no="11", nm="架空 三郎", pos="SF", av=.91, min=27.6, fg2a=6.4, fg3a=3.9, fta=3.1,
      fg2p=.541, fg3p=.352, ftp=.773, oreb=1.2, dreb=3.4, ast=1.9, tov=1.5, stl=0.8, blk=0.4, pf=2.6, fd=2.6,
      e_min=5.0, e_pts=4.2, e_reb=1.9, e_ast=1.1),
 dict(no="24", nm="架空 四郎", pos="PF", av=.89, min=26.8, fg2a=8.2, fg3a=1.4, fta=4.6,
      fg2p=.558, fg3p=.301, ftp=.702, oreb=2.4, dreb=5.1, ast=1.4, tov=1.9, stl=0.6, blk=0.9, pf=3.0, fd=3.8,
      e_min=4.9, e_pts=4.6, e_reb=2.4, e_ast=0.9),
 dict(no="32", nm="架空 五郎", pos="C",  av=.87, min=24.5, fg2a=7.0, fg3a=0.4, fta=3.4,
      fg2p=.589, fg3p=.268, ftp=.648, oreb=2.9, dreb=5.8, ast=1.1, tov=1.7, stl=0.5, blk=1.3, pf=3.2, fd=3.0,
      e_min=4.7, e_pts=4.0, e_reb=2.6, e_ast=0.8),
 dict(no="3",  nm="架空 六郎", pos="SG", av=.78, min=18.9, fg2a=3.2, fg3a=4.1, fta=1.5,
      fg2p=.489, fg3p=.366, ftp=.788, oreb=0.4, dreb=1.9, ast=1.8, tov=1.1, stl=0.7, blk=0.1, pf=1.8, fd=1.3,
      e_min=6.1, e_pts=3.4, e_reb=1.4, e_ast=1.0),
 dict(no="15", nm="架空 七郎", pos="PF", av=.72, min=16.4, fg2a=3.8, fg3a=1.1, fta=1.9,
      fg2p=.512, fg3p=.318, ftp=.726, oreb=1.3, dreb=2.6, ast=0.8, tov=0.9, stl=0.4, blk=0.5, pf=2.2, fd=1.6,
      e_min=5.5, e_pts=3.1, e_reb=1.6, e_ast=0.7),
 dict(no="41", nm="架空 八郎", pos="C",  av=.64, min=14.2, fg2a=3.1, fg3a=0.2, fta=1.4,
      fg2p=.566, fg3p=.240, ftp=.617, oreb=1.5, dreb=2.7, ast=0.5, tov=0.8, stl=0.3, blk=0.8, pf=2.4, fd=1.2,
      e_min=5.9, e_pts=2.8, e_reb=1.7, e_ast=0.6),
 dict(no="5",  nm="架空 九郎", pos="PG", av=.57, min=12.0, fg2a=2.0, fg3a=1.8, fta=0.9,
      fg2p=.471, fg3p=.330, ftp=.755, oreb=0.3, dreb=1.2, ast=2.0, tov=1.0, stl=0.5, blk=0.1, pf=1.5, fd=0.8,
      e_min=6.4, e_pts=2.6, e_reb=1.1, e_ast=1.0),
]

# ── 架空ブルズ（アウェイ・予想78点） ────────────────────────────
away = [
 dict(no="9",  nm="架空 一馬", pos="PG", av=.94, min=30.1, fg2a=6.9, fg3a=5.9, fta=3.2,
      fg2p=.498, fg3p=.402, ftp=.821, oreb=0.5, dreb=2.4, ast=5.6, tov=2.4, stl=1.0, blk=0.2, pf=2.3, fd=2.7,
      e_min=5.6, e_pts=4.7, e_reb=1.9, e_ast=1.6),
 dict(no="21", nm="架空 二馬", pos="SF", av=.92, min=29.3, fg2a=7.4, fg3a=4.2, fta=4.1,
      fg2p=.533, fg3p=.361, ftp=.758, oreb=1.4, dreb=3.9, ast=2.1, tov=1.8, stl=0.9, blk=0.5, pf=2.7, fd=3.4,
      e_min=5.1, e_pts=4.9, e_reb=2.2, e_ast=1.1),
 dict(no="8",  nm="架空 三馬", pos="SG", av=.88, min=26.2, fg2a=4.6, fg3a=6.1, fta=1.8,
      fg2p=.476, fg3p=.374, ftp=.804, oreb=0.4, dreb=2.2, ast=2.6, tov=1.4, stl=1.2, blk=0.1, pf=2.0, fd=1.5,
      e_min=5.4, e_pts=4.3, e_reb=1.6, e_ast=1.3),
 dict(no="33", nm="架空 四馬", pos="C",  av=.85, min=25.4, fg2a=7.6, fg3a=0.3, fta=3.8,
      fg2p=.571, fg3p=.255, ftp=.631, oreb=3.1, dreb=6.0, ast=1.0, tov=1.9, stl=0.4, blk=1.5, pf=3.3, fd=3.2,
      e_min=4.8, e_pts=4.1, e_reb=2.7, e_ast=0.7),
 dict(no="12", nm="架空 五馬", pos="PF", av=.83, min=23.7, fg2a=5.8, fg3a=2.1, fta=2.6,
      fg2p=.524, fg3p=.329, ftp=.714, oreb=1.9, dreb=4.2, ast=1.3, tov=1.5, stl=0.6, blk=0.7, pf=2.8, fd=2.2,
      e_min=4.9, e_pts=3.8, e_reb=2.1, e_ast=0.8),
 dict(no="4",  nm="架空 六馬", pos="PG", av=.74, min=19.8, fg2a=3.4, fg3a=3.6, fta=1.6,
      fg2p=.481, fg3p=.344, ftp=.769, oreb=0.3, dreb=1.7, ast=2.9, tov=1.3, stl=0.8, blk=0.1, pf=1.9, fd=1.4,
      e_min=5.8, e_pts=3.3, e_reb=1.2, e_ast=1.2),
 dict(no="17", nm="架空 七馬", pos="SF", av=.69, min=17.1, fg2a=3.5, fg3a=2.4, fta=1.3,
      fg2p=.503, fg3p=.311, ftp=.742, oreb=0.8, dreb=2.3, ast=1.0, tov=1.0, stl=0.5, blk=0.3, pf=2.1, fd=1.1,
      e_min=5.7, e_pts=3.0, e_reb=1.4, e_ast=0.8),
 dict(no="45", nm="架空 八馬", pos="C",  av=.61, min=15.3, fg2a=3.3, fg3a=0.2, fta=1.7,
      fg2p=.549, fg3p=.230, ftp=.598, oreb=1.7, dreb=2.9, ast=0.6, tov=0.9, stl=0.3, blk=0.9, pf=2.6, fd=1.5,
      e_min=6.0, e_pts=2.9, e_reb=1.8, e_ast=0.6),
 dict(no="6",  nm="架空 九馬", pos="SG", av=.53, min=13.1, fg2a=2.2, fg3a=1.9, fta=0.8,
      fg2p=.462, fg3p=.318, ftp=.731, oreb=0.3, dreb=1.4, ast=1.1, tov=0.8, stl=0.4, blk=0.1, pf=1.6, fd=0.7,
      e_min=6.3, e_pts=2.5, e_reb=1.2, e_ast=0.7),
]

build(home, 84.0)
build(away, 78.0)

TH_FG, TH_2P3P, TH_FT = 4.0, 3.0, 3.0   # 率を出す試投数の閾値（詳細設計 3.3）

def f1(x): return f"{x:.1f}"
def pc(x): return f"{x*100:.1f}%"

def rows(team, side):
    out = []
    for i, p in enumerate(team):
        pid = f"{side}{i}"
        # 率は閾値以上のときだけ出す。未満は分数のみ
        fgp  = pc(p["fgp"]) if p["fga"]  >= TH_FG   else None
        p2   = pc(p["fg2p"]) if p["fg2a"] >= TH_2P3P else None
        p3   = pc(p["fg3p"]) if p["fg3a"] >= TH_2P3P else None
        pft  = pc(p["ftp"])  if p["fta"]  >= TH_FT   else None
        efg  = pc(p["efg"]) if p["fga"] >= TH_FG else None
        ts   = pc(p["ts"])  if p["fga"] >= TH_FG else None
        def frac(m, a, r):
            return f'{f1(m)} / {f1(a)}' + (f' <span class="pct">({r})</span>' if r else '')
        out.append(f'''      <tbody>
        <tr class="r1">
          <th scope="rowgroup" class="c-nm">
            <button type="button" class="pl" id="b-{pid}" aria-expanded="false" aria-controls="d-{pid}">
              <span class="num no">{p["no"]}</span><span class="nm">{p["nm"]}</span><span class="chev" aria-hidden="true"></span>
            </button>
          </th>
          <td class="c-pos">{p["pos"]}</td>
          <td class="c-n num">{f1(p["min"])}</td>
          <td class="c-n num st">{f1(p["pts"])}</td>
          <td class="c-n num">{f1(p["reb"])}</td>
          <td class="c-n num">{f1(p["ast"])}</td>
        </tr>
        <tr class="r2" id="d-{pid}" hidden>
          <td colspan="6">
            <dl class="box">
              <div><dt>出場時間</dt><dd class="num">{f1(p["min"])}<span class="u">分</span> <span class="err">±{f1(p["e_min"])}</span></dd></div>
              <div><dt>得点</dt><dd class="num st">{f1(p["pts"])} <span class="err">±{f1(p["e_pts"])}</span></dd></div>
              <div><dt>リバウンド</dt><dd class="num">{f1(p["reb"])} <span class="err">±{f1(p["e_reb"])}</span> <span class="sub">OR {f1(p["oreb"])} / DR {f1(p["dreb"])}</span></dd></div>
              <div><dt>アシスト</dt><dd class="num">{f1(p["ast"])} <span class="err">±{f1(p["e_ast"])}</span></dd></div>
              <div class="hr"><dt>フィールドゴール</dt><dd class="num">{frac(p["fgm"], p["fga"], fgp)}</dd></div>
              <div class="in"><dt>2ポイント</dt><dd class="num">{frac(p["fg2m"], p["fg2a"], p2)}</dd></div>
              <div class="in"><dt>3ポイント</dt><dd class="num">{frac(p["fg3m"], p["fg3a"], p3)}</dd></div>
              <div><dt>フリースロー</dt><dd class="num">{frac(p["ftm"], p["fta"], pft)}</dd></div>
              <div class="hr"><dt>ターンオーバー</dt><dd class="num">{f1(p["tov"])}</dd></div>
              <div><dt>スティール</dt><dd class="num">{f1(p["stl"])}<span class="lo"> 誤差大</span></dd></div>
              <div><dt>ブロック</dt><dd class="num">{f1(p["blk"])}<span class="lo"> 誤差大</span></dd></div>
              <div><dt>ファウル</dt><dd class="num">{f1(p["pf"])}</dd></div>
              <div><dt>被ファウル</dt><dd class="num">{f1(p["fd"])}</dd></div>
              <div class="hr"><dt>EFG%・TS%</dt><dd class="num">{(efg + " ・ " + ts) if efg else "—"}</dd></div>
              <div><dt>出場確率</dt><dd class="num">{p["av"]*100:.0f}%</dd></div>
            </dl>
          </td>
        </tr>
      </tbody>''')
    # 合計は「画面に出ている値を足した数」にする。丸める前の値で出すと、
    # 読者が列を足した結果と合計行が食い違う（同じ画面に矛盾した数字が並ぶ）。
    tm = sum(round(p["min"], 1) for p in team); tp = sum(round(p["pts"], 1) for p in team)
    tr = sum(round(p["reb"], 1) for p in team); ta = sum(round(p["ast"], 1) for p in team)
    out.append(f'''      <tbody class="tot">
        <tr>
          <th scope="rowgroup" class="c-nm"><span class="totnm">合計（{len(team)}名）</span></th>
          <td class="c-pos"></td>
          <td class="c-n num">{f1(tm)}</td>
          <td class="c-n num st">{f1(tp)}</td>
          <td class="c-n num">{f1(tr)}</td>
          <td class="c-n num">{f1(ta)}</td>
        </tr>
      </tbody>''')
    return "\n".join(out), tm, tp

hrows, hmin, hpts = rows(home, "h")
arows, amin, apts = rows(away, "a")

open("_home_rows.html","w").write(hrows)
open("_away_rows.html","w").write(arows)

print(f"HOME  min={hmin:.4f}  pts={hpts:.6f}   (表示丸め後 min={sum(round(p['min'],1) for p in home):.1f} pts={sum(round(p['pts'],1) for p in home):.1f})")
print(f"AWAY  min={amin:.4f}  pts={apts:.6f}   (表示丸め後 min={sum(round(p['min'],1) for p in away):.1f} pts={sum(round(p['pts'],1) for p in away):.1f})")
for t,n in ((home,"home"),(away,"away")):
    bad = [p["nm"] for p in t if not (p["fg2m"]<=p["fg2a"] and p["fg3m"]<=p["fg3a"] and p["ftm"]<=p["fta"])]
    print(f"{n}: 制約違反 {len(bad)}件")
