"""エージェント交代の足場（AGENTS.md / docs/STATUS.md）の検証。

**規約の二重化を機械的に防ぐ。** `AGENTS.md` に絶対ルールを書き写すと、片方だけ
古くなって「どちらが正か」が分からなくなる。入口は入口のままに保つ。
"""
from __future__ import annotations

import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
AGENTS = REPO_ROOT / "AGENTS.md"
STATUS = REPO_ROOT / "docs" / "STATUS.md"
CLAUDE = REPO_ROOT / "CLAUDE.md"


def test_entry_points_exist():
    """Codex は `AGENTS.md`、Claude Code は `CLAUDE.md` を読む。両方が要る。"""
    for path in (AGENTS, STATUS, CLAUDE):
        assert path.exists(), f"{path.name} がない"


def test_agents_points_at_the_single_source_of_rules():
    """`AGENTS.md` は規約の正本（CLAUDE.md）と現在地を**リンクで**指すこと。

    **名前が本文のどこかにあるだけでは足りない。** 「最初に読むもの」から辿れないと、
    交代したエージェントが規約に行き着かない。実際、単なる出現の検査では
    リンクを消しても通ってしまった（ファイル一覧の表に名前が残るため）。
    """
    body = AGENTS.read_text(encoding="utf-8")
    for target in ("CLAUDE.md", "docs/STATUS.md"):
        assert f"]({target})" in body, f"{target} へのリンクがない"


def test_agents_does_not_restate_the_absolute_rules():
    """**`AGENTS.md` に絶対ルールを書き写さない。**

    写した瞬間に二重管理になり、片方だけ古くなる。入口であって規約の置き場ではない。
    ここが緩むと「Claude 用の規約」と「Codex 用の規約」が別々に育つ。
    """
    body = AGENTS.read_text(encoding="utf-8")
    assert "## 絶対ルール" not in body
    # CLAUDE.md の絶対ルールの見出しが1つでも現れたら、写し始めている
    headings = re.findall(r"^### \d+\. (.+)$", CLAUDE.read_text(encoding="utf-8"), re.MULTILINE)
    assert headings, "CLAUDE.md の絶対ルールの見出しが見つからない（検査が空振りしている）"
    copied = [h for h in headings if h in body]
    assert not copied, f"AGENTS.md に絶対ルールが写っている: {copied}"


def test_status_has_no_version_number():
    """`STATUS.md` は状態であって設計ではない。**版数を持たせない。**

    版数を付けると設計文書のように扱われ、「更新したら版数を上げる」規律が要る。
    状態は変わるたびに上書きするものなので、その規律は合わない。
    """
    body = STATUS.read_text(encoding="utf-8")
    assert "| 版数 |" not in body
    assert not re.search(r"^\*\*版数", body, re.MULTILINE)


@pytest.mark.parametrize(
    "heading",
    ["いま何をしているか", "運営者の判断待ち", "本番の状態"],
)
def test_status_keeps_the_sections_that_cannot_be_derived(heading: str):
    """リポジトリから導けない情報の置き場が消えていないこと。

    これらが無くなると、交代したエージェントが「本番に何が入っているか」を
    知る手段を失う（`git log` にも設計文書にも書いていない）。
    """
    assert heading in STATUS.read_text(encoding="utf-8")


def test_claude_md_links_to_the_handoff():
    """`CLAUDE.md` からも交代の手順に辿れること。

    Claude Code は `AGENTS.md` を自動では読まない。リンクがないと片側通行になる。
    """
    body = CLAUDE.read_text(encoding="utf-8")
    assert "AGENTS.md" in body
    assert "docs/STATUS.md" in body
