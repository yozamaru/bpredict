"""工程3: GitHub Actions の設定の検証。

`permissions` の既定や `next lint` の不在は、書き忘れても動いてしまう種類の欠陥である。
**YAML パーサを入れずにテキストで検査する** — 見ているのは行の有無と語の有無だけで、
構造の解釈が要らない。CI 設定のためだけに依存を1つ増やす理由がない。
"""
from __future__ import annotations

import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

# 静的JSONとスナップショットをコミットするため write が必要なワークフロー
# （CLAUDE.md 絶対ルール4 / 詳細設計 4.1）。それ以外に write を与えない。
MAY_WRITE = {"daily-ingest.yml", "gameday-update.yml"}


def workflows() -> list[pathlib.Path]:
    return sorted(WORKFLOW_DIR.glob("*.yml"))


def test_workflow_dir_exists():
    assert workflows(), ".github/workflows に *.yml がない"


@pytest.mark.parametrize("path", workflows(), ids=lambda p: p.name)
def test_every_workflow_declares_permissions(path):
    """`permissions` を書かないワークフローを作らない（CLAUDE.md 絶対ルール4）。

    省略すると既定が適用され、リポジトリ設定次第で write が付く。
    """
    body = path.read_text(encoding="utf-8")
    assert re.search(r"^permissions:", body, re.MULTILINE), f"{path.name}: permissions がない"
    assert re.search(r"^\s+contents:\s*read\s*$", body, re.MULTILINE), \
        f"{path.name}: contents: read がない"


@pytest.mark.parametrize("path", workflows(), ids=lambda p: p.name)
def test_only_ingest_workflows_get_write(path):
    body = path.read_text(encoding="utf-8")
    has_write = bool(re.search(r"^\s+contents:\s*write", body, re.MULTILINE))
    if path.name in MAY_WRITE:
        return
    assert not has_write, f"{path.name}: contents: write は {sorted(MAY_WRITE)} 以外に与えない"


@pytest.mark.parametrize("path", workflows(), ids=lambda p: p.name)
def test_every_workflow_sets_concurrency_and_timeout(path):
    """走行時間の上限と同時実行の制御を必ず書く（詳細設計 4.1）。"""
    body = path.read_text(encoding="utf-8")
    assert re.search(r"^concurrency:", body, re.MULTILINE), f"{path.name}: concurrency がない"
    assert "timeout-minutes:" in body, f"{path.name}: timeout-minutes がない"


def without_comments(path: pathlib.Path) -> str:
    """コメント行を落とした本文を返す。

    「`next lint` を使わない」という説明をコメントに書くのは正しいので、
    コマンドとしての出現だけを見る。
    """
    return "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


@pytest.mark.parametrize("path", workflows(), ids=lambda p: p.name)
def test_no_next_lint(path):
    """`next lint` は Next.js 16 で削除された。`eslint .` を直接呼ぶ（CLAUDE.md）。

    書いてもコマンドが存在せず失敗するか、将来の互換シムに依存することになる。
    """
    assert "next lint" not in without_comments(path), f"{path.name}: next lint がある"


@pytest.mark.parametrize("path", workflows(), ids=lambda p: p.name)
def test_third_party_actions_are_pinned_by_sha(path):
    """サードパーティ Action はコミットSHAでピン留めする（基本設計 7.3）。

    `actions/*` は GitHub 公式であり、タグ参照を許す。
    """
    for ref in re.findall(r"uses:\s*(\S+)", path.read_text(encoding="utf-8")):
        owner = ref.split("/", 1)[0]
        if owner == "actions":
            continue
        assert re.search(r"@[0-9a-f]{40}$", ref), f"{path.name}: SHA でピンされていない: {ref}"


def test_ci_runs_on_push_and_pull_request():
    body = (WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8")
    assert "push:" in body and "pull_request:" in body


def test_ci_runs_ruff_mypy_and_pytest():
    """Python 側の3点が CI に入っていること（基本設計 7.2）。"""
    body = (WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8")
    for cmd in ("ruff check batch", "mypy", "pytest batch/tests"):
        assert cmd in body, f"ci.yml に {cmd} がない"


def test_ci_checks_fixtures_and_static_file_count():
    """A-12（fixtures の検査）と A-14（out/ のファイル数）が CI にあること。"""
    body = (WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8")
    assert "scripts/check_fixtures.py" in body
    assert "18000" in body


def test_canary_cron_is_utc_for_0700_jst():
    """cron は UTC で書く。07:00 JST = 22:00 UTC（CLAUDE.md 時刻の扱い）。"""
    body = (WORKFLOW_DIR / "parser-canary.yml").read_text(encoding="utf-8")
    assert "cron: '0 22 * * *'" in body


def test_dependabot_covers_pip_and_actions():
    """pip と github-actions を weekly で登録する（基本設計 7.3）。

    npm は api / web の package.json ができた時点で追記する。存在しない
    ディレクトリを指定すると Dependabot がエラーを出し続ける。
    """
    body = (REPO_ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    assert "package-ecosystem: pip" in body
    assert "package-ecosystem: github-actions" in body
    assert body.count("interval: weekly") >= 2
