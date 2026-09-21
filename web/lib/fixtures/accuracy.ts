// **表示確認用の合成データ。** 実際の成績ではない。
import type { CalibrationPoint } from '@/components/charts/CalibrationChart';

export const SAMPLE_OVERALL = {
  n: 312,
  correct: 223,
  accuracy: 0.714,
  baselineAccuracy: 0.601,
  brier: 0.204,
  logloss: 0.598,
};

export const SAMPLE_BY_SEASON = [
  { season: '2024-25', accuracy: 0.671, n: 540 },
  { season: '2025-26', accuracy: 0.694, n: 528 },
  { season: '2026-27', accuracy: 0.714, n: 312 },
];

export const SAMPLE_CALIBRATION: CalibrationPoint[] = [
  { bucket: '50-60%', predicted: 0.55, actual: 0.52, n: 74 },
  { bucket: '60-70%', predicted: 0.65, actual: 0.636, n: 88 },
  { bucket: '70-80%', predicted: 0.75, actual: 0.77, n: 81 },
  { bucket: '80-90%', predicted: 0.85, actual: 0.84, n: 52 },
];

export const SAMPLE_BY_PROVISIONAL = {
  provisional: { accuracy: 0.68, n: 120 },
  confirmed: { accuracy: 0.73, n: 192 },
};
