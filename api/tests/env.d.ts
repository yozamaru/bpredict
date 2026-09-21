// Workers ランタイムでテストを走らせるための型。
//
// `worker-configuration.d.ts` は `wrangler types` が生成する（手で直さない）。
// シークレットは wrangler.toml に書かないため型に現れないので、ここで足す。
/// <reference types="@cloudflare/vitest-pool-workers/types" />

declare namespace Cloudflare {
  interface Env {
    INGEST_TOKEN?: string;
    INGEST_TOKEN_NEXT?: string;
    FINALIZE_TOKEN?: string;
    /** `db/migrations/*.sql` を `readD1Migrations` で読んだもの（vitest.config.ts が注入する）。 */
    TEST_MIGRATIONS: import('@cloudflare/vitest-pool-workers').D1Migration[];
  }
}
