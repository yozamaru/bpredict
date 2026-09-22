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


def test_canary_gate_does_not_depend_on_event_payload():
    """カナリアの分岐をイベントのペイロードに依存させない（工程5）。

    `github.event.repository.default_branch` が schedule イベントで空になると、
    条件が偽になって**毎日サイレントにスキップされる**。カナリアはサイト構造の変更を
    検知する唯一の手段（基本設計 7.2）であり、永久にグリーンになるのは検知したい
    壊れ方そのものである。既定ブランチ名はリテラルで書く。
    """
    body = (WORKFLOW_DIR / "parser-canary.yml").read_text(encoding="utf-8")
    gate = re.search(r"^\s+if: (.+)$", body, re.MULTILINE)
    assert gate, "parser-canary.yml: ジョブの分岐条件がない"
    assert gate[1].strip() == "github.ref == 'refs/heads/main'", \
        "parser-canary.yml: 分岐はリテラルの既定ブランチ名で書く"
    assert "github.event." not in gate[1]


def test_canary_skip_is_visible_and_does_not_fetch():
    """規約ハッシュ未設定のスキップを警告として残す（工程5）。

    notice は run の一覧に出ないため、設定し忘れたまま何ヶ月も気づけない。
    スキップ自体は正しい（運営者が robots / 規約を確認するまで通信しない）。
    """
    body = (WORKFLOW_DIR / "parser-canary.yml").read_text(encoding="utf-8")
    assert "::warning::" in body
    assert "SCRAPER_ROBOTS_SHA256" in body and "SCRAPER_TERMS_SHA256" in body


def test_dependabot_covers_pip_and_actions():
    """pip と github-actions を weekly で登録する（基本設計 7.3）。

    npm は api / web の package.json ができた時点で追記する。存在しない
    ディレクトリを指定すると Dependabot がエラーを出し続ける。
    """
    body = (REPO_ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    assert "package-ecosystem: pip" in body
    assert "package-ecosystem: github-actions" in body
    assert body.count("interval: weekly") >= 2


def test_backfill_is_manual_only_and_serialized():
    """backfill は手動実行のみ・D1 書き込みの直列化・300分（詳細設計 4.1 / 4.8）。

    `schedule` を足すと1日1シーズンという制限（相手サイトへの負荷と D1 書込枠の
    両方）が自動実行で破られる。`concurrency: d1-write` がないと `daily-ingest`
    と重なって `revision` の採番が競合する。
    """
    path = WORKFLOW_DIR / "backfill.yml"
    body = path.read_text(encoding="utf-8")
    stripped = without_comments(path)
    assert "workflow_dispatch:" in stripped
    assert "schedule:" not in stripped, "backfill に定期実行を足さない"
    assert re.search(r"^\s+group: d1-write$", body, re.MULTILINE)
    assert re.search(r"^\s+cancel-in-progress: false$", body, re.MULTILINE), \
        "走行中を殺すと ingestion_logs が RUNNING のまま残る"
    assert "timeout-minutes: 300" in body


def test_backfill_gate_is_default_branch_literal():
    """別ブランチのコードで本番 D1 へ書かない。分岐はリテラルで書く。"""
    body = (WORKFLOW_DIR / "backfill.yml").read_text(encoding="utf-8")
    gate = re.search(r"^\s+if: (.+)$", body, re.MULTILINE)
    assert gate and gate[1].strip() == "github.ref == 'refs/heads/main'"


def test_backfill_fails_when_unconfigured():
    """未設定のまま通信しない。**skip ではなく fail** にする（絶対ルール6）。

    手動実行なので、黙って通り過ぎるよりその場で気づける方がよい。空の
    User-Agent や規約ハッシュ未設定で取得すると作法を破る。
    """
    body = (WORKFLOW_DIR / "backfill.yml").read_text(encoding="utf-8")
    assert "::error::" in body
    for name in ("API_BASE_URL", "INGEST_TOKEN", "SCRAPER_USER_AGENT",
                 "SCRAPER_ROBOTS_SHA256", "SCRAPER_TERMS_SHA256"):
        assert name in body, f"backfill.yml: {name} の確認がない"


def test_backfill_seeds_masters_before_ingesting():
    """マスタ投入が backfill より前にあること（詳細設計 8.1）。

    `club_source_ids` がないと全試合がクラブ解決に失敗して落ちる。
    `seed_master` は冪等（ON CONFLICT）なので毎回流してよい。
    """
    body = (WORKFLOW_DIR / "backfill.yml").read_text(encoding="utf-8")
    assert body.index("batch.jobs.seed_master") < body.index("batch.jobs.backfill")


@pytest.mark.parametrize("path", workflows(), ids=lambda p: p.name)
def test_inputs_are_not_interpolated_into_run(path):
    """`inputs.*` を `run:` の本文へ直接展開しない。

    展開はシェルに解釈される前に置換されるため、値がそのままコマンドとして
    走る余地が残る。`env:` 経由で渡し、シェル変数として参照する。
    """
    body = path.read_text(encoding="utf-8")
    blocks = re.findall(r"^\s+run: \|?\s*\n((?:[ \t]{10,}.*\n)+)", body, re.MULTILINE)
    blocks += re.findall(r"^\s+run: (?!\|)(.+)$", body, re.MULTILINE)
    assert blocks, f"{path.name}: run が1つも見つからない（検査が空振りしている）"
    for block in blocks:
        assert "inputs." not in block, f"{path.name}: run の中で inputs を展開している"
