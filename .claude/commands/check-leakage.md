---
description: データリークの有無を検査する
---

特徴量にデータリークがないか検査してください。

```bash
pytest batch/tests/test_leakage.py -v
```

その上で、`batch/features/` 配下の全関数について以下を目視確認する。

1. `as_of` を引数に取っているか
2. 参照条件が **`finished_at <= as_of`** になっているか（`tipoff_at` ではない）
3. `status = 'SCHEDULED'` の試合を除外しているか
4. 対象試合自身の `team_game_stats` / `player_game_stats` / `games.attendance` を参照していないか
5. チーム所属の判定に `players` の現在の所属を使っていないか（`player_game_stats.club_id` か `player_seasons` を使う）

直近の walk-forward スコアを確認し、的中率が 75% を超えていたらリークを疑って原因を特定する。85% 超はほぼ確実にリークがある。

問題があれば箇所と修正案を報告する。**勝手に直さず、まず報告する。**

参照: `.claude/skills/feature-engineering/SKILL.md`
