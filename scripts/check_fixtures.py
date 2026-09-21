#!/usr/bin/env python3
"""fixtures に実サイト由来の文字列が混入していないことを検査する（受け入れ基準 A-12）。

取得したレスポンス本文を public リポジトリに置くことは、「事実データだから」という
本プロジェクトの立論が一切通用しない唯一の行為である（要件 4.5.2）。`.gitignore` だけに
頼らず、**追跡されているファイル**を機械的に検査する。

fixtures がまだ無い段階では何も検出せずに通る（工程5で実物が入る）。
"""
from __future__ import annotations

import csv
import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# 生のレスポンス本文。fixtures 配下かどうかを問わず追跡してはならない
FORBIDDEN_SUFFIXES = (".html", ".raw.json", ".html.gz")

# 抽出対象外であることを固定する（要件 5.3）。fixture にも含めない
FORBIDDEN_KEYS = (
    "RefereeName",      # 審判名。特徴量 #30 は見送りで、人物の個人情報でもある
    "RefereeID",
    "SubRefereeID",
    "ActionCD",         # プレイバイプレイ
    "PlayText",
    "ShotChart",
)


def tracked_files() -> list[pathlib.Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [REPO_ROOT / p for p in out.split("\0") if p]


def real_club_names() -> set[str]:
    """`clubs.csv` の名称を「実サイト由来」の判定に使う。

    fixture は値をダミーに置換した合成データであり、実在クラブ名が現れてはならない
    （要件 4.5.2）。出典が確定しているこの30件を検出語彙にする。
    """
    path = REPO_ROOT / "db" / "seeds" / "master" / "clubs.csv"
    if not path.exists():
        return set()
    with path.open(encoding="utf-8", newline="") as f:
        return {r["name"].strip() for r in csv.DictReader(f) if r.get("name", "").strip()}


def main() -> int:
    problems: list[str] = []
    files = tracked_files()

    # 1. 生のレスポンス本文が追跡されていないこと
    for path in files:
        name = path.name
        if any(name.endswith(s) for s in FORBIDDEN_SUFFIXES):
            rel = path.relative_to(REPO_ROOT)
            problems.append(f"生のレスポンス本文が追跡されている: {rel}")

    # 2. fixtures の中身
    fixtures = [p for p in files if "fixtures" in p.parts]
    names = real_club_names()
    for path in fixtures:
        try:
            body = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue                      # バイナリは対象外
        rel = path.relative_to(REPO_ROOT)
        for key in FORBIDDEN_KEYS:
            if key in body:
                problems.append(f"抽出対象外のフィールドが fixture にある: {rel} ({key})")
        for club in names:
            if club in body:
                problems.append(f"実在クラブ名が fixture にある: {rel} ({club})")

    print(f"追跡ファイル {len(files)} 件 / fixtures {len(fixtures)} 件 / 検出語彙 {len(names)} 件")
    if problems:
        print("\n".join(f"  NG  {p}" for p in sorted(set(problems))), file=sys.stderr)
        return 1
    print("OK  実サイト由来の文字列は検出されなかった")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
