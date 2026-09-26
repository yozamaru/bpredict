import type { NextConfig } from 'next';

// 静的出力。ISR は Cloudflare Pages 上で成立しないため使わない（要件 7章）。
const nextConfig: NextConfig = {
  output: 'export',
  // 静的ホスティングのため画像最適化を使わない（そもそも画像を配信しない）
  images: { unoptimized: true },
  // 末尾スラッシュを付け、Pages の静的配信とパスの対応を単純にする
  trailingSlash: true,
};

export default nextConfig;

import('@opennextjs/cloudflare').then(m => m.initOpenNextCloudflareForDev());
