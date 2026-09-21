"""終了済み試合1件で埋め込みJSONと必須項目を検査する。DBには書かない。"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from batch.jobs.seed_master import load_club_source_ids
from batch.parser.boxscore_parser import parse_boxscore
from batch.scraper.boxscore import boxscore_url
from batch.scraper.client import RateLimitedClient

DEFAULT_STATE_PATH = Path("batch/.scraper-state/state.json")


class CanaryConfigurationError(ValueError):
    """取得に必要な設定が未完了。通信する前に停止する。"""


def _baseline(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
        raise CanaryConfigurationError("承認済みの規約ハッシュを設定してください")
    return value.lower()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="終了済みボックススコア1件の構造を確認する")
    ap.add_argument("--game-id", default="505497", help="確認済みの終了試合の公式ID")
    ap.add_argument("--event", type=int, choices=(2, 3), default=2, help="2: リーグ戦、3: CS")
    ap.add_argument(
        "--inspect-policy", action="store_true",
        help="robots/規約のハッシュ候補のみ表示する。基準値の保存・承認はしない",
    )
    args = ap.parse_args(argv)

    try:
        # 未設定・不正な基準値で通常実行を始めない。確認モードだけ候補取得を許す。
        robots = None if args.inspect_policy else _baseline("SCRAPER_ROBOTS_SHA256")
        terms = None if args.inspect_policy else _baseline("SCRAPER_TERMS_SHA256")
        client = RateLimitedClient(
            user_agent=os.environ.get("SCRAPER_USER_AGENT", ""),
            state_path=Path(os.environ.get("SCRAPER_STATE_PATH", str(DEFAULT_STATE_PATH))),
            robots_sha256=robots,
            terms_sha256=terms,
        )
        if args.inspect_policy:
            print(json.dumps(client.inspect_policy(), sort_keys=True))
            return 0

        url = boxscore_url(args.game_id)
        clubs = {row.source_id: row.club_id for row in load_club_source_ids()}
        client.verify_policy()
        result = parse_boxscore(
            client.get(url), event=args.event, clubs=clubs, expected_game_id=args.game_id,
        )
        print(f"parser_canary: OK (teams={len(result.teams)}, players={len(result.players)})")
        return 0
    except Exception as exc:  # noqa: BLE001 — 公開ログの境界で全例外の本文を除去する。
        # 公開ActionsログへURL・本文・元の通信例外やスタックトレースを出さない。
        print(f"parser_canary: 確認失敗 ({type(exc).__name__})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
