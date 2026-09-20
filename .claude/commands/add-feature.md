---
description: 新しい特徴量を追加する
---

以下の特徴量を追加してください。

$ARGUMENTS

手順:

1. `docs/requirements.md` 6.2 を読み、既に「見送り」に区分されていないか確認する。されていれば理由を提示して着手前に確認を取る（天候・直接対戦成績・標高・オッズは方針上の見送りであり、技術的な問題ではない）
2. その値が**試合開始時点で確定しているか**を検証する。していなければ「過去試合の集計」への変換方法を提案する
3. `batch/features/` に実装する
   - `as_of` を必ず引数に取る
   - 絞り込みは `finished_at <= as_of`（`tipoff_at` ではない）
   - `status = 'SCHEDULED'` を除外する
   - 可能なら `diff` に集約する（列数を半減できる）
   - 欠損時は None を返す（関数内で0埋めしない）
4. `docs/requirements.md` 6.2 と `docs/design-detail.md` 2.2 に追記する
5. `pytest batch/tests/test_leakage.py -v` を実行する
6. 寄与度（SHAP / gain）と**欠損率**を測定して報告する
7. 効果がなければ削除を提案する

**一度に複数を追加しない。** 1つずつ足して walk-forward Brier の改善を測る。8,000行に対して既に60〜70列あり、初期 fold（1,600行）では過剰になっている。入れることより、効かないものを落とすことに労力を使う。

参照: `.claude/skills/feature-engineering/SKILL.md`
