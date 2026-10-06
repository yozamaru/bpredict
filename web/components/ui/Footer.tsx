import Link from 'next/link';

import { SITE_VERSION } from '@/lib/version';

/**
 * 全ページ共通フッター。免責・出典・/about への到達性と**版**を常設する
 * （要件 F-12 / A-07）。
 * 出典の文言は要件 4.5.5 の確定文言をそのまま出す。短縮・言い換えをしない。
 *
 * **版は `lib/version.ts` から読む**（ここに書き写さない。要件 8.2）。
 */
export function Footer() {
  return (
    <footer className="mt-8 border-t border-rule py-4 text-[11px] leading-relaxed text-ink-3">
      <p>
        データ出典: B.LEAGUE 公式サイト（
        <a href="https://www.bleague.jp/" className="underline">
          https://www.bleague.jp/
        </a>
        ）で公表されている試合日程・記録を基に、本サイトが独自に集計・加工したものです。
      </p>
      <p className="mt-2">
        本サイトは公益社団法人ジャパン・プロフェッショナル・バスケットボールリーグ（B.LEAGUE）
        および各クラブとは一切関係のない、個人が運営する非公式サービスです。
        予測内容について B.LEAGUE および各クラブへお問い合わせいただくことはご遠慮ください。
      </p>
      <nav aria-label="このサイトについて" className="mt-2 flex gap-4">
        <Link href="/about/" className="flex min-h-11 items-center font-bold text-ink-2 underline">
          免責・出典・個人情報の取扱い
        </Link>
      </nav>
      {/* 版（要件 F-12）。**数字は等幅で組む**（ui-implementation の規約）。
          リンクにしない — 遷移先が無い */}
      <p className="mt-1 font-mono tabular-nums">{SITE_VERSION}</p>
    </footer>
  );
}
