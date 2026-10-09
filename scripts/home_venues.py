"""各クラブの本拠地・表記揺れ・代替会場を1行にまとめる（調査用。1回だけ）。

**出典はスナップショットだけ**（絶対ルール3）。D1 を読まない。
**名寄せはしない** — 同じ建物と思われる別IDは「推定」として別の列に出し、
本拠の判定には使わない（詳細設計 1.1）。

    python3 scripts/home_venues.py verification/home_venues.csv

**出力先に既定を置かない。** うっかり上書きしないため、引数で明示させる。
結果の読み方は `verification/RESULTS.md` の末尾にある。
"""
from __future__ import annotations

import collections
import csv
import sys
from pathlib import Path

import pandas as pd

SNAP = Path("batch/snapshot")
CUR, PREV = "2026-27-PREMIER", "2025-26-B1"

games = pd.read_parquet(SNAP / "games.parquet")
clubs = pd.read_parquet(SNAP / "club_seasons.parquet")
venues = pd.read_parquet(SNAP / "venues.parquet")
revisions = pd.read_parquet(SNAP / "venue_revisions.parquet")

name_of = {str(r.id): str(r.name) for r in venues.itertuples()}

# 会場ごとの名称履歴（同一IDでの改称。**事実**である）
history: dict[str, list[str]] = {}
for vid, grp in revisions.groupby("venue_id"):
    seen: list[str] = []
    for row in grp.sort_values("valid_from").itertuples():
        if str(row.name) not in seen:
            seen.append(str(row.name))
    history[str(vid)] = seen

# 「同じ建物と思われる別ID」の候補。**名称からの推定であり、本拠の判定に使わない。**
# 括弧の中（公式の施設名が入る）が一致する組だけを拾う
def bare(label: str) -> str | None:
    for left, right in (("（", "）"), ("(", ")")):
        if left in label and label.endswith(right):
            return label[label.index(left) + 1 : -1].strip() or None
    return None

building: dict[str, set[str]] = collections.defaultdict(set)
for vid, labels in list(history.items()) + [(k, [v]) for k, v in name_of.items()]:
    for label in labels:
        for key in filter(None, (bare(label), label)):
            building[key].add(vid)


def home_games(season: str, club: str) -> collections.Counter[str]:
    """そのクラブのホーム戦の会場（**予定を含む**）。会場IDが無い試合は落ちる。"""
    rows = games[(games.season_id == season) & (games.home_club_id == club)]
    return collections.Counter(
        str(r.venue_id) for r in rows.itertuples()
        if r.venue_id is not None and str(r.venue_id) not in ("", "nan", "None")
    )


def label(vid: str, count: int) -> str:
    return f"{name_of.get(vid, vid)}({count})"


rows: list[dict[str, object]] = []
for club in clubs[clubs.season_id == CUR].sort_values("club_id").itertuples():
    cid = str(club.club_id)
    cur, prev = home_games(CUR, cid), home_games(PREV, cid)
    # **当季に1試合でもあれば当季。無ければ前季。閾値を発明しない**
    source, basis_label = (cur, "当季") if cur else (prev, "前季")
    if not source:
        rows.append({"クラブ": club.name, "略称": club.short_name, "本拠地": "",
                     "会場ID": "", "表記揺れ": "", "代替会場": "",
                     "根拠": "ホーム戦が1件も無い", "注意": ""})
        continue
    vid, n = source.most_common(1)[0]

    others = [label(k, c) for k, c in cur.most_common() if k != vid]
    others += [label(k, c) for k, c in prev.most_common() if k != vid and k not in cur]

    aliases = [a for a in history.get(vid, []) if a != name_of.get(vid)]
    same = sorted(
        {o for key in filter(None, (bare(name_of.get(vid, "")), name_of.get(vid)))
         for o in building.get(key, set()) if o != vid}
    )

    # **このクラブが使った会場のすべての組**で、名称の包含を推定に入れる
    # （`ブレックスアリーナ宇都宮` ⊂ `とちぎん・ブレックスアリーナ宇都宮`）。
    # クラブで絞るのは、短い名称が無関係な会場に当たるのを避けるためである。
    # **選んだ会場との比較だけでは足りない** — 同じ建物が代替会場の側に2つ並ぶ
    used = sorted(set(cur) | set(prev))
    pairs: list[tuple[str, str]] = []
    for i, a in enumerate(used):
        for b in used[i + 1 :]:
            na, nb = name_of.get(a, ""), name_of.get(b, "")
            if na and nb and (na in nb or nb in na):
                pairs.append((a, b))
    # 選んだ会場を含む組は `same`（本拠の別ID）、それ以外は `pairs`（代替会場同士）
    same = sorted(set(same) | {o for pair in pairs if vid in pair
                               for o in pair} - {vid})
    pairs = [pair for pair in pairs if vid not in pair]

    notes: list[str] = []
    if basis_label == "前季":
        notes.append("当季のホーム戦が無く、前季から採った")
    elif prev and prev.most_common(1)[0][0] != vid:
        top, count = prev.most_common(1)[0]
        notes.append(f"前季の最頻は {name_of.get(top)}（{count}試合）")
    if same:
        notes.append("同じ建物と思われる別ID: " + " / ".join(
            f"{o}={name_of.get(o, o)}" for o in same) + "（名称からの推定）")
    for a, b in pairs:
        notes.append(f"{name_of.get(a)}({a}) と {name_of.get(b)}({b}) は"
                     "同じ建物と思われる（名称からの推定）")

    shared = sorted(
        other.short_name for other in clubs[clubs.season_id == CUR].itertuples()
        if str(other.club_id) != cid
        and vid in home_games(CUR, str(other.club_id))
    )
    if shared:
        notes.append("当季この会場を本拠とする他クラブ: " + " / ".join(shared))

    rows.append({
        "クラブ": club.name,
        "略称": club.short_name,
        "本拠地": name_of.get(vid, vid),
        "会場ID": vid,
        "表記揺れ": " / ".join(aliases),
        "代替会場": " / ".join(others),
        "根拠": f"{basis_label} {n}試合（当季 {sum(cur.values())} / 前季 {sum(prev.values())}）",
        "注意": "。".join(notes),
    })

out = Path(sys.argv[1])
with out.open("w", encoding="utf-8-sig", newline="") as fh:
    writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
print(f"{len(rows)}クラブ -> {out}")
