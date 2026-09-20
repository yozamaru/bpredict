// P0-15  Zod による入力検証の CPU コストを測る。
//
// 05 は「手書き検証」での測定だった（500行で 1.34ms）。Zod は数倍遅いと見込んでいるが、
// 実測していない。Workers Free の CPU 予算は 1リクエスト 10ms。
//
// 前提: 捨てディレクトリで zod を入れる
//   mkdir -p /tmp/zodbench && cd /tmp/zodbench && npm init -y >/dev/null && npm i zod
//   node <このリポジトリ>/verification/09_zod_validation_cost.mjs
//
// 注意: Node と workerd は同じ V8 だが CPU の計上方法が違う。ここで出るのは「桁と倍率」。
//       最終確認は 07 の Worker をデプロイし、Cloudflare のダッシュボードで CPU を見る。

let z;
try {
  ({ z } = await import('zod'));
} catch {
  console.error('zod が解決できません。捨てディレクトリで npm i zod してから実行してください。');
  console.error('  mkdir -p /tmp/zodbench && cd /tmp/zodbench && npm init -y >/dev/null && npm i zod');
  process.exit(1);
}

const id = z.string().min(1).max(64);
const int = (lo, hi) => z.number().int().min(lo).max(hi);

// docs/design-detail.md 1.3 の player_game_stats（24列）
const PlayerGameStat = z.object({
  game_id: id, player_id: id, club_id: id,
  game_date: z.string().regex(/^\d{4}-\d{2}-\d{2}$/),
  started: int(0, 1), minutes: z.number().min(0).max(60),
  fg2m: int(0, 40), fg2a: int(0, 60), fg3m: int(0, 30), fg3a: int(0, 50),
  ftm: int(0, 40), fta: int(0, 50), oreb: int(0, 30), dreb: int(0, 40),
  ast: int(0, 30), tov: int(0, 20), stl: int(0, 15), blk: int(0, 15),
  pf: int(0, 6), fd: int(0, 20), plus_minus: int(-99, 99), pts: int(0, 250),
  fetched_at: z.string(),
}).superRefine((r, ctx) => {                      // 恒等式（要件 5.3）
  if (r.fg2m > r.fg2a || r.fg3m > r.fg3a || r.ftm > r.fta)
    ctx.addIssue({ code: 'custom', message: '成功数が試投数を超えている' });
  if (r.pts !== r.fg2m * 2 + r.fg3m * 3 + r.ftm)
    ctx.addIssue({ code: 'custom', message: '得点が恒等式を満たさない' });
});

// docs/design-detail.md 1.5 の player_predictions（31列・最も列数が多い）
const rate = z.number().min(0).max(1);
const PlayerPrediction = z.object({
  id: id, prediction_id: id, game_id: id, player_id: id, club_id: id,
  model_version: id, revision: int(1, 9999), predicted_at: z.string(),
  avail_prob: rate, pred_minutes: z.number().min(0).max(60),
  pred_fg2a: z.number().min(0), pred_fg3a: z.number().min(0), pred_fta: z.number().min(0),
  pred_fg2_pct: rate, pred_fg3_pct: rate, pred_ft_pct: rate,
  pred_oreb: z.number().min(0), pred_dreb: z.number().min(0), pred_ast: z.number().min(0),
  pred_tov: z.number().min(0), pred_stl: z.number().min(0), pred_blk: z.number().min(0),
  pred_pf: z.number().min(0), pred_fd: z.number().min(0),
  err_minutes: z.number().nullable(), err_pts: z.number().nullable(),
  err_reb: z.number().nullable(), err_ast: z.number().nullable(),
  is_provisional: int(0, 1), is_final: int(0, 1), is_active: int(0, 1),
});

const makeStat = (i) => {
  const fg2m = 4, fg3m = 2, ftm = 3;
  return { game_id: `g${i}`, player_id: `p${i}`, club_id: `c${i % 12}`,
    game_date: '2026-09-22', started: i % 2, minutes: 24.5,
    fg2m, fg2a: 8, fg3m, fg3a: 5, ftm, fta: 4, oreb: 1, dreb: 3,
    ast: 4, tov: 2, stl: 1, blk: 0, pf: 2, fd: 3, plus_minus: 5,
    pts: fg2m * 2 + fg3m * 3 + ftm, fetched_at: '2026-09-22T10:00:00Z' };
};
const makePred = (i) => ({
  id: `pp${i}`, prediction_id: 'p1', game_id: 'g1', player_id: `pl${i}`, club_id: 'c1',
  model_version: 'winner-v1.0.0', revision: 1, predicted_at: '2026-09-22T10:00:00Z',
  avail_prob: 0.9, pred_minutes: 28.4,
  pred_fg2a: 7.8, pred_fg3a: 5.3, pred_fta: 3.9,
  pred_fg2_pct: 0.526, pred_fg3_pct: 0.434, pred_ft_pct: 0.846,
  pred_oreb: 0.6, pred_dreb: 2.5, pred_ast: 6.1, pred_tov: 2.2,
  pred_stl: 1.1, pred_blk: 0.3, pred_pf: 2.4, pred_fd: 3.1,
  err_minutes: 5.8, err_pts: 4.8, err_reb: 2.1, err_ast: 1.5,
  is_provisional: 0, is_final: 0, is_active: 1,
});

function bench(label, schema, make, rows, budgetNote) {
  const payload = JSON.stringify({ rows: Array.from({ length: rows }, (_, i) => make(i)) });
  const arr = z.array(schema);
  for (let i = 0; i < 20; i++) arr.parse(JSON.parse(payload).rows);   // ウォームアップ
  const samples = [];
  for (let i = 0; i < 50; i++) {
    const t0 = process.hrtime.bigint();
    const parsed = JSON.parse(payload);
    const ok = arr.safeParse(parsed.rows);
    const t1 = process.hrtime.bigint();
    if (!ok.success) throw new Error(`検証に失敗: ${label}`);
    samples.push(Number(t1 - t0) / 1e6);
  }
  samples.sort((a, b) => a - b);
  const p50 = samples[25], p95 = samples[47];
  const kb = (payload.length / 1024).toFixed(0);
  const flag = p95 > 10 ? '★予算超過' : p95 > 5 ? '危険(>50%)' : 'OK';
  console.log(`${label.padEnd(32)} ${String(rows).padStart(5)}行 ${kb.padStart(6)}KB  `
    + `p50 ${p50.toFixed(2).padStart(6)}ms  p95 ${p95.toFixed(2).padStart(6)}ms  ${flag}  ${budgetNote}`);
  return p95;
}

console.log('='.repeat(108));
console.log('P0-15  Zod の検証コスト（Workers Free の CPU 予算 = 1リクエスト 10ms）');
console.log('='.repeat(108));
console.log(`zod ${(await import('zod/package.json', { with: { type: 'json' } }).catch(() => ({ default: { version: '?' } }))).default.version} / Node ${process.version}`);
console.log('計測: JSON.parse → Zod による型・値域・恒等式の検証');
console.log('注意: Node 実測。workerd とは CPU 計上が異なるため「桁と倍率」として扱う');
console.log('');
console.log('対象'.padEnd(32) + '  行数   サイズ         中央値         p95     判定');
console.log('-'.repeat(108));

const worst = [];
// 1リクエストあたりの上限行数（docs/design-detail.md 3.4）で測る
worst.push(bench('player_game_stats (24列)', PlayerGameStat, makeStat, 160, '← 1リクエスト上限'));
worst.push(bench('player_predictions (31列)', PlayerPrediction, makePred, 120, '← 1リクエスト上限'));
console.log('');
console.log('参考（05 と比較するための 500行）');
console.log('-'.repeat(108));
bench('player_game_stats (24列)', PlayerGameStat, makeStat, 500, '← 05 は手書き検証で 1.34ms');

console.log('');
console.log('【判断材料】');
const w = Math.max(...worst);
if (w > 10) {
  console.log(`  ★ 上限行数での p95 が ${w.toFixed(2)}ms で、CPU 予算 10ms を超える。`);
  console.log('    → 手書き検証へ差し替えるか、1リクエストあたりの行数をさらに下げる');
  console.log('    → docs/design-detail.md 3.4 と design-basic 7.5 を先に直してから実装する');
} else if (w > 5) {
  console.log(`  上限行数での p95 が ${w.toFixed(2)}ms。予算内だが半分を超えている。`);
  console.log('    → Zod を採用してよいが、スキーマを重くする変更のたびに再測定する');
} else {
  console.log(`  上限行数での p95 が ${w.toFixed(2)}ms。CPU 予算 10ms に対して余裕がある。`);
  console.log('    → 設計どおり Zod を採用する。手書き検証への差し替えは不要');
}
console.log('');
console.log('  実機での最終確認: 07 の Worker をデプロイし、Cloudflare の Workers → Metrics で');
console.log('  CPU 時間の p99 を見る。Node の数値はあくまで桁の目安。');
console.log('');
console.log('verification/RESULTS.md の P0-15 の行に、上限行数での p95 を記録すること。');
