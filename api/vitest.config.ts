import { cloudflareTest, readD1Migrations } from '@cloudflare/vitest-pool-workers';
import { defineConfig } from 'vitest/config';

// テストは Workers ランタイム（Miniflare）で走らせる。
// `crypto.subtle.timingSafeEqual` と D1 の `batch()` は Workers の API であり、
// Node 上では検証できない。
//
// **スキーマは db/migrations/*.sql を唯一の出典とする**（CLAUDE.md）。
// テスト側に DDL を書き写さない。
const migrations = await readD1Migrations('../db/migrations');

export default defineConfig({
  test: {
    // **テストファイル間で D1 が共有される。** このプールのバージョンには
    // isolated storage の指定がないため、並行実行すると片方の DELETE が
    // もう片方の挿入を消し、原因の分かりにくい失敗になる。直列に走らせる。
    fileParallelism: false,
  },
  plugins: [
    cloudflareTest({
      wrangler: { configPath: './wrangler.toml' },
      miniflare: { bindings: { TEST_MIGRATIONS: migrations } },
    }),
  ],
});
