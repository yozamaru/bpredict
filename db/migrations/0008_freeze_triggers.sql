-- 確定予測の凍結トリガ（親子に例外を設けない）
-- 出典: docs/design-detail.md 1章。**このファイルは適用後に編集しない。**
-- 変更は新しい番号のファイルを追加して行う（CLAUDE.md「マイグレーションは追記のみ」）。

CREATE TRIGGER trg_predictions_final_immutable
BEFORE UPDATE ON predictions
WHEN OLD.is_final = 1
BEGIN
  SELECT RAISE(ABORT, 'final prediction is immutable');
END;

CREATE TRIGGER trg_predictions_final_nodelete
BEFORE DELETE ON predictions
WHEN OLD.is_final = 1
BEGIN
  SELECT RAISE(ABORT, 'final prediction cannot be deleted');
END;

-- 子テーブルにも例外なく同じ凍結を適用する
CREATE TRIGGER trg_ppred_final_immutable
BEFORE UPDATE ON player_predictions
WHEN OLD.is_final = 1
BEGIN
  SELECT RAISE(ABORT, 'final player prediction is immutable');
END;

CREATE TRIGGER trg_ppred_final_nodelete
BEFORE DELETE ON player_predictions
WHEN OLD.is_final = 1
BEGIN
  SELECT RAISE(ABORT, 'final player prediction cannot be deleted');
END;

-- player_predictions は自身の is_final に加えて、親の確定でも守る。
-- 自テーブルの列だけで守ると、freeze が子への UPDATE を取りこぼした場合に
-- 「親は確定済みなのに子は書き換えられる」状態が残る。
CREATE TRIGGER trg_ppred_parent_final_immutable
BEFORE UPDATE ON player_predictions
WHEN (SELECT is_final FROM predictions WHERE id = OLD.prediction_id) = 1
BEGIN
  SELECT RAISE(ABORT, 'player predictions of a final prediction are immutable');
END;

CREATE TRIGGER trg_ppred_parent_final_nodelete
BEFORE DELETE ON player_predictions
WHEN (SELECT is_final FROM predictions WHERE id = OLD.prediction_id) = 1
BEGIN
  SELECT RAISE(ABORT, 'player predictions of a final prediction cannot be deleted');
END;

-- prediction_reasons は is_final を持たないため、親を参照して判定する
CREATE TRIGGER trg_reasons_final_immutable
BEFORE UPDATE ON prediction_reasons
WHEN (SELECT is_final FROM predictions WHERE id = OLD.prediction_id) = 1
BEGIN
  SELECT RAISE(ABORT, 'reasons of a final prediction are immutable');
END;

CREATE TRIGGER trg_reasons_final_nodelete
BEFORE DELETE ON prediction_reasons
WHEN (SELECT is_final FROM predictions WHERE id = OLD.prediction_id) = 1
BEGIN
  SELECT RAISE(ABORT, 'reasons of a final prediction cannot be deleted');
END;

CREATE TRIGGER trg_team_targets_final_immutable
BEFORE UPDATE ON prediction_team_targets
WHEN (SELECT is_final FROM predictions WHERE id = OLD.prediction_id) = 1
BEGIN
  SELECT RAISE(ABORT, 'team targets of a final prediction are immutable');
END;

CREATE TRIGGER trg_team_targets_final_nodelete
BEFORE DELETE ON prediction_team_targets
WHEN (SELECT is_final FROM predictions WHERE id = OLD.prediction_id) = 1
BEGIN
  SELECT RAISE(ABORT, 'team targets of a final prediction cannot be deleted');
END;

CREATE TRIGGER trg_bundle_final_immutable
BEFORE UPDATE ON prediction_model_bundle
WHEN (SELECT is_final FROM predictions WHERE id = OLD.prediction_id) = 1
BEGIN
  SELECT RAISE(ABORT, 'model bundle of a final prediction is immutable');
END;

CREATE TRIGGER trg_bundle_final_nodelete
BEFORE DELETE ON prediction_model_bundle
WHEN (SELECT is_final FROM predictions WHERE id = OLD.prediction_id) = 1
BEGIN
  SELECT RAISE(ABORT, 'model bundle of a final prediction cannot be deleted');
END;
