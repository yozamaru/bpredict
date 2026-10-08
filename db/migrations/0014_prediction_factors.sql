-- この予測に使った項目（詳細設計 1.5 / 2.7.2 / 3.3。2026-10-09）
--
-- 運営者の指摘は「**なぜこの予測になったかが2項目しかないので、どの項目から
-- この予測を導き出したのかを詳しく知りたい**」である。
--
-- `prediction_reasons` は**要因グループ4つに集約した寄与**を持ち、現在の21列では
-- **2件しか出ない**（`VENUE` に該当列が1本もなく、`PLAYER` の3列は定数で寄与が
-- 厳密に 0。2.7.1）。グループを増やすには特徴量そのものを増やすしかない。
--
-- **そこで「寄与」ではなく「使った項目の事実」を別に持つ**（運営者が3案から選んだ）。
-- 有利不利を主張しないため**打ち消しの問題が起きず、21列すべてを出せる**。
--
-- **`prediction_reasons` に混ぜない。** あちらは「なぜそうなったか」の主張で、
-- こちらは「何を見たか」の列挙である。`favors TEXT NOT NULL CHECK (... IN
-- ('HOME','AWAY'))` は向きの無い列（`series_game_no` など）を表せない。
--
-- **`feature_snapshot` から API が組み立てる案を採らない。** 文言表（2.7.1 の21行）が
-- Python と TypeScript の両方に必要になり、**片方だけ直したときに画面が静かに
-- ずれる**（静的JSON 側は Python のままなので、3つ目の写しは作らない）。
CREATE TABLE prediction_factors (
  prediction_id TEXT NOT NULL REFERENCES predictions(id),
  -- 並び順。**寄与の大きさではない**（要因グループの順 → 列の順）。
  -- 寄与で並べると「どれがどれだけ効いたか」を主張することになり、
  -- この表が避けている話に戻る
  rank          INTEGER NOT NULL,
  group_key     TEXT NOT NULL
                CHECK (group_key IN ('TEAM_STRENGTH','SCHEDULE','PLAYER','VENUE')),
  label_ja      TEXT NOT NULL,
  value_text    TEXT NOT NULL,
  -- **値が大きい側。** 「有利な側」ではない（`prediction_reasons.favors` と
  -- 書き分ける）。係数が負の列（`drtg_diff`）では両者が逆を向く。
  -- 向きを持たない列（`series_game_no` / `entry_is_official` / 差が 0）は NULL
  larger        TEXT CHECK (larger IS NULL OR larger IN ('HOME','AWAY')),
  PRIMARY KEY (prediction_id, rank)
);

-- 凍結に例外を設けない（絶対ルール2 / 詳細設計 1.8）。`is_final` 列を持たない
-- 子テーブルは**親を参照して**守る（`prediction_reasons` と同じ）。
CREATE TRIGGER trg_factors_final_immutable
BEFORE UPDATE ON prediction_factors
WHEN (SELECT is_final FROM predictions WHERE id = OLD.prediction_id) = 1
BEGIN
  SELECT RAISE(ABORT, 'factors of a final prediction are immutable');
END;

CREATE TRIGGER trg_factors_final_nodelete
BEFORE DELETE ON prediction_factors
WHEN (SELECT is_final FROM predictions WHERE id = OLD.prediction_id) = 1
BEGIN
  SELECT RAISE(ABORT, 'factors of a final prediction cannot be deleted');
END;
