/**
 * Cloudflare Workers のエントリポイント。
 *
 * 役割は「書き込みの関門」と、静的JSONで賄えない動的クエリの配信、そして
 * バッチが必要とする運用上の読み取り（基本設計 2.4）。
 *
 * 主要導線（今日の予測・試合一覧）はここを通らない。バッチが書き出した静的JSONを
 * Pages が配信する（無料枠が枯渇しても当日の予測が表示され続ける）。
 */
import { Hono } from 'hono';

import { fail } from './lib/http';
import { auth } from './middleware/auth';
import { facts } from './routes/internal/facts';
import { finalize, freeze } from './routes/internal/finalize';
import { masters } from './routes/internal/masters';
import { metrics, models } from './routes/internal/models';
import { ops } from './routes/internal/ops';
import { predictions } from './routes/internal/predictions';

export type Env = {
  DB: D1Database;
  INGEST_TOKEN?: string;
  INGEST_TOKEN_NEXT?: string;
  FINALIZE_TOKEN?: string;
};

// **ベースパスは `/api/v1`**（詳細設計 3.1 の「すべて」に内部エンドポイントも含む）。
// WAF のレートリミットは `/api/v1/*` に当てるため、ここに載っていないと保護の外に出る。
const app = new Hono<{ Bindings: Env }>().basePath('/api/v1');

// **トークンは用途で分離する。** `/internal/*` に一括で当てない。
// `/internal/finalize` は破壊的操作のため `FINALIZE_TOKEN` を使う（詳細設計 3.4）。
// 一括適用にすると、エンドポイントを足したときに誤って INGEST_TOKEN で通る。
const ingest = auth(['INGEST_TOKEN', 'INGEST_TOKEN_NEXT']);

app.use('/internal/masters', ingest);
app.use('/internal/games', ingest);
app.use('/internal/games/*', ingest);
app.use('/internal/stats', ingest);
app.use('/internal/entries', ingest);
app.use('/internal/ratings', ingest);
app.use('/internal/venue-revisions', ingest);
app.use('/internal/predictions', ingest);
app.use('/internal/predictions/*', ingest);
app.use('/internal/evaluate', ingest);
app.use('/internal/summary', ingest);
app.use('/internal/log', ingest);
app.use('/internal/models', ingest);
app.use('/internal/models/*', ingest);
app.use('/internal/metrics/*', ingest);
// freeze だけは別トークン
app.use('/internal/finalize', auth(['FINALIZE_TOKEN']));

app.route('/internal/masters', masters);
app.route('/internal', facts);                     // /games /stats /entries /ratings
app.route('/internal/predictions', predictions);   // POST / と GET /pending
app.route('/internal/finalize', finalize);
app.route('/internal/models', models);             // POST / と GET /active /:version/artifact
app.route('/internal/metrics', metrics);           // GET /internal/metrics/active
app.route('/internal', ops);                       // /evaluate /summary /log /games/ingested

app.notFound((c) => fail(c, 'NOT_FOUND', '該当するエンドポイントがない'));

/**
 * **例外オブジェクトをそのままレスポンス・ログに入れない**（CLAUDE.md 絶対ルール4）。
 * D1 REST API の URL にはアカウントIDとDB IDが含まれるため、`str(e)` を公開すると
 * それらが漏れる。型名と自前の短いメッセージに限定する。
 */
app.onError((err, c) => {
  console.error(`unhandled: ${err.constructor.name}`);
  return fail(c, 'INTERNAL', '内部エラー');
});

export default {
  fetch: app.fetch,

  /**
   * freeze は Workers の Cron Trigger（毎時）で行う。
   *
   * GitHub Actions ではなく Workers に置くのは、外部アクセスを必要としない純粋な
   * D1 操作であり、**スクレイピングの失敗に巻き込まれてはならない**ため
   * （基本設計 4.1）。旧版は翌朝の日次ジョブ内で実行され、試合開始から約11時間
   * freeze されない状態が毎日生じていた。
   */
  scheduled(_controller: ScheduledController, env: Env, ctx: ExecutionContext) {
    ctx.waitUntil(
      (async () => {
        try {
          const r = await freeze(env.DB);
          console.log(`finalize: frozen=${r.frozen.length} remaining=${r.remaining}`);
        } catch (err) {
          console.error(`finalize failed: ${(err as Error).constructor.name}`);
        }
      })(),
    );
  },
} satisfies ExportedHandler<Env>;
