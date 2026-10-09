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
    # **`contents` を必ず明示する。** 既定は `read` で、`MAY_WRITE` だけ `write`
    # を許す（基本設計 7.3）。**書いていないことを許さない** — 省略すると
    # リポジトリ設定次第で write が付く
    allowed = r"read|write" if path.name in MAY_WRITE else r"read"
    assert re.search(rf"^\s+contents:\s*({allowed})\s*$", body, re.MULTILINE), \
        f"{path.name}: contents: {allowed.replace('|', ' か ')} がない"


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


def test_dry_run_writes_nothing_to_d1():
    """**`dry_run` は D1 に1行も書かない。**

    入力の説明は「取得はするが D1 には書かない」である。ところが `seed_master` の
    ステップが無条件に走っており、診断のたびにマスタを書いて当日の書き込み枠を
    削っていた（1回あたり約200行。2026-09-26 に400行を無駄にした）。

    枠は00:00 UTC にしか戻らないため、診断で削ると**その日に取り込める試合数が
    そのぶん減る**（詳細設計 4.8）。
    """
    body = (WORKFLOW_DIR / "backfill.yml").read_text(encoding="utf-8")
    step = re.search(r"^(\s+)- name: マスタの投入.*\n((?:\1  .*\n)+)", body, re.MULTILINE)
    assert step, "backfill.yml: マスタ投入のステップが見つからない"
    assert "inputs.dry_run" in step[2], \
        "backfill.yml: マスタ投入が dry_run で守られていない（D1 に書いてしまう）"


def test_backfill_carries_the_exclusion_list_home():
    """**取り込まない試合の一覧を持ち帰る**（要件 5.3 / 詳細設計 4.8）。

    `backfill` は `contents: read` でコミットできないため、実行環境で書いた
    `batch/exclusions/excluded_games.json` はジョブ終了とともに消える。実際に
    2022-23 の `除外=1` を1回失った。**権限を増やさず artifact で持ち帰る。**

    `if: always()` でなければ、PARTIAL で終わった回の分が落ちる。
    """
    body = (WORKFLOW_DIR / "backfill.yml").read_text(encoding="utf-8")
    step = re.search(
        r"^(\s+)- name: 取り込まない試合の一覧を持ち帰る\n((?:\1  .*\n)+)", body, re.MULTILINE)
    assert step, "backfill.yml: 一覧を持ち帰るステップがない"
    assert "upload-artifact" in step[2]
    assert "batch/exclusions/excluded_games.json" in step[2]
    assert "if: always()" in step[2], "PARTIAL の回の分が落ちる"
    # **権限は read のまま。** 一覧のためにワークフローへ write を与えない（絶対ルール4）。
    # **コメント中の文字列を拾わないよう、`permissions` ブロックだけを見る**
    granted = re.search(r"^permissions:\n((?:[ \t]+.*\n)+)", body, re.MULTILINE)
    assert granted, "backfill.yml: permissions がない"
    assert "contents: write" not in granted[1]


# --- cron を置く条件（詳細設計 4.2） ---

DAILY_SLOTS = ("17 2 * * *", "17 6 * * *", "17 10 * * *", "17 14 * * *")


def _daily_crons() -> list[str]:
    body = (WORKFLOW_DIR / "daily-ingest.yml").read_text(encoding="utf-8")
    return re.findall(r"- cron: '([^']+)'", body)


def test_daily_ingest_runs_four_times_a_day():
    """**1日4回回す**（運営者の指示。時刻は 2026-10-10 に変えた）。

    cron は UTC で書く（CLAUDE.md 時刻の扱い）。
    **11:17 / 15:17 / 19:17 / 23:17 JST = 02:17 / 06:17 / 10:17 / 14:17 UTC。**
    4つとも同じ UTC 日に収まるため、曜日指定のずらしは要らない。
    """
    assert _daily_crons() == list(DAILY_SLOTS)


def test_no_slot_is_on_the_hour():
    """**分を `0` にしない。**

    GitHub の文書が「遅れを減らすには毎時0分以外の時刻に設定すること」と
    勧めている。**実測で1日4回のうち2回しか発火していなかった**
    （詳細設計 4.2）。`0` に戻す変更をここで止める。
    """
    minutes = [c.split()[0] for c in _daily_crons()]
    assert "0" not in minutes, minutes


def test_each_slot_is_its_own_cron_expression():
    """**4スロットを別々の式で書く。**

    ① `github.event.schedule` は cron 式をそのまま返すため、1つの式に複数の
       時刻を書くと**スロットが同じ文字列になり、ステップ1b の分岐が死ぬ**。
    ② **実測で、単独の式は4日連続で発火し、3時刻を1つの式に書いた側は毎日
       1回しか届いていなかった**（詳細設計 4.2）。
    """
    crons = _daily_crons()
    assert len(crons) == 4, crons
    for expr in crons:
        minute, hour = expr.split()[0], expr.split()[1]
        assert "," not in hour and "/" not in hour, expr
        assert "," not in minute and "/" not in minute, expr


def test_only_the_first_slot_walks_the_upcoming_schedule():
    """**最初のスロット以外ではステップ1b を回さない**（walk を1回節約する）。

    要件 5.2「取得は必要最小限のページに限る」。未実施の試合の追加・延期は
    1日1回の反映で足りる。
    """
    body = (WORKFLOW_DIR / "daily-ingest.yml").read_text(encoding="utf-8")
    gate = re.search(r"github\.event\.schedule == '([^']+)'", body)
    assert gate is not None
    assert gate.group(1) == DAILY_SLOTS[0], gate.group(1)


def test_the_schedule_run_turns_on_every_step():
    """**`schedule` では `inputs` が空になる。**

    真偽値の入力も `''` になるため、`inputs.yesterday` をそのまま使うと
    **全ステップが false になりジョブがエラーで終わる**。定期実行で何も
    取り込まない状態に静かになるのを防ぐ。
    """
    body = (WORKFLOW_DIR / "daily-ingest.yml").read_text(encoding="utf-8")
    for name in ("results", "settle", "inference"):
        assert f"github.event_name == 'schedule' || inputs.{name}" in body, name


def test_the_schedule_run_commits_its_output():
    """定期実行でもスナップショットと静的JSON をコミットすること。

    `!inputs.dry_run` は `schedule` では `!''` = true になるため動くが、
    **偶然に頼らない**（`dry_run` の既定値が変わったら静かに止まる）。
    """
    body = (WORKFLOW_DIR / "daily-ingest.yml").read_text(encoding="utf-8")
    assert "github.event_name == 'schedule' || !inputs.dry_run" in body


def test_train_runs_monthly():
    """cron は UTC で書く。毎月1日 19:00 UTC = JST 2日 04:00（設計 4.1）。

    **採用判定が現行モデルと比較するようになってから置いた**（詳細設計 4.6）。
    それまでは条件1〜2 が課されておらず、**定期実行すると月次で悪いモデルが
    自動採用されうる**状態だった。
    """
    body = (WORKFLOW_DIR / "train.yml").read_text(encoding="utf-8")
    assert "cron: '0 19 1 * *'" in body


def test_the_scheduled_train_makes_a_version_from_the_date():
    """**同じ版を2回登録すると主キー違反で落ちる**（詳細設計 4.5.1）。

    既定の `1.0.0` のままだと、2回目の月次で必ず落ちる。
    """
    body = (WORKFLOW_DIR / "train.yml").read_text(encoding="utf-8")
    assert 'MODEL_VERSION="1.$(date -u +%Y).$(date -u +%m%d)"' in body


def test_train_does_not_register_by_default():
    """**手動実行では登録を既定にしない**（詳細設計 4.5.1）。

    登録は D1 の書き込み枠を使い、同じ版を2回送ると主キー違反で落ちる。
    **定期実行では登録する**（そのために置いた cron である）。
    """
    body = (WORKFLOW_DIR / "train.yml").read_text(encoding="utf-8")
    block = body.split("register:", 1)[1].split("model_version:", 1)[0]
    assert "default: false" in block, "train.yml の register の既定が false でない"


def test_daily_ingest_commits_the_player_index():
    """**`web/data` をコミットする**（詳細設計 4.14）。

    `web/data/players.csv` は `/players/[id]` の静的生成の出典であり、
    **コミットされなければ次のビルドで選手ページが0件になる** — 画面は壊れず、
    リンクも `canLink` で消えるため、**気づく手がかりが無い**。

    実際に漏れていた（2026-10-06。集計ジョブを足したときに `git add` の一覧を
    直し忘れた）。
    """
    body = (WORKFLOW_DIR / "daily-ingest.yml").read_text(encoding="utf-8")
    add = body.split("for path in", 1)[1].split(";", 1)[0]
    for path in ("batch/snapshot", "web/public/data", "web/data", "batch/exclusions"):
        assert path in add, f"daily-ingest.yml が {path} をコミットしない"


def test_the_deploy_is_gated_on_an_actual_commit():
    """**コミットが行われた回だけ配る**（詳細設計 4.2 のステップ6）。

    無条件にすると、試合の無い日も含めて1日4回同じものを配り直す。
    `secrets` を `if:` に書かないことも併せて固定する — **文脈の可否に依存すると、
    常に偽になっても「黙って配らないだけ」で気づけない**。
    """
    body = (WORKFLOW_DIR / "daily-ingest.yml").read_text(encoding="utf-8")
    assert "name: Pages へ配る" in body, "daily-ingest.yml が Pages へ配らない"

    gate = "if: steps.commit.outputs.deploy == 'true'"
    # setup-node と配る段の2つに掛かっている（トークンが無い回はビルドもしない）
    assert body.count(gate) == 2, f"配る段の条件が {body.count(gate)} 箇所（2 のはず）"

    # **`if:` に `secrets` を書かない。** 判定は commit 段の出力だけを見る
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(("if:", "- if:")):
            assert "secrets." not in stripped, f"if: に secrets を書いている: {stripped}"


def test_the_commit_step_reports_both_outcomes():
    """**コミット段は配るかどうかを必ず出力する。**

    「変更なし」で `exit 0` する枝で出力を書き忘れると、`steps.commit.outputs.deploy`
    が空になり **配る段が黙って飛ばされる**。失敗ではなく無反応になるため、
    ログを見ない限り気づけない。
    """
    body = (WORKFLOW_DIR / "daily-ingest.yml").read_text(encoding="utf-8")
    step = body.split("name: スナップショットと静的JSON と選手一覧をコミット", 1)[1]
    step = step.split("- uses: actions/setup-node", 1)[0]
    assert step.count('deploy=false') == 2, "変更なしの枝とトークン未登録の枝の両方で false を書く"
    assert step.count('deploy=true') == 1
    # トークンの有無はここで見る（`-n` を見るだけで、値をログに出さない）
    assert 'if [[ -n "$PAGES_TOKEN" ]]' in step
    assert "PAGES_TOKEN: ${{ secrets.CF_PAGES_TOKEN }}" in body


def test_the_deploy_passes_the_output_directory_explicitly():
    """**`web/` で `wrangler pages` を引数なしに実行しない**（詳細設計 8.1）。

    Next.js を検出して `opennextjs-cloudflare build` を走らせ、採用していない
    構成（ISR が R2/KV を要求する）の生成物を作る。
    """
    body = (WORKFLOW_DIR / "daily-ingest.yml").read_text(encoding="utf-8")
    assert "wrangler pages deploy out --project-name bpredict --branch main" in body
    # 認証は専用トークン。`CF_API_TOKEN`（D1 Edit）では配れない（基本設計 7.4）
    assert "CLOUDFLARE_API_TOKEN: ${{ secrets.CF_PAGES_TOKEN }}" in body
    assert "CLOUDFLARE_ACCOUNT_ID: ${{ vars.CLOUDFLARE_ACCOUNT_ID }}" in body
