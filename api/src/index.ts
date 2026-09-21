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

import { auth } from './middleware/auth';
import { masters, type Env } from './routes/internal/masters';

const app = new Hono<{ Bindings: Env }>();

// 内部エンドポイントは Bearer 必須。2キー方式で無停止回転できる（基本設計 7.4）
app.use('/internal/*', auth(['INGEST_TOKEN', 'INGEST_TOKEN_NEXT']));
app.route('/internal/masters', masters);

app.notFound((c) =>
  c.json({ error: { code: 'NOT_FOUND', message: '該当するエンドポイントがない' } }, 404),
);

/**
 * **例外オブジェクトをそのままレスポンス・ログに入れない**（CLAUDE.md 絶対ルール4）。
 * D1 REST API の URL にはアカウントIDとDB IDが含まれるため、`str(e)` を公開すると
 * それらが漏れる。型名と自前の短いメッセージに限定する。
 */
app.onError((err, c) => {
  console.error(`unhandled: ${err.constructor.name}`);
  return c.json({ error: { code: 'INTERNAL', message: '内部エラー' } }, 500);
});

export default app;
