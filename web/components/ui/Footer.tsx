import Link from 'next/link';

import { SITE_COPYRIGHT, SITE_VERSION } from '@/lib/version';

/**
 * 全ページ共通フッター。免責・出典・/about への到達性と**著作権表示**・**版**を
 * 常設する（要件 F-12 / A-07）。
 * 出典の文言は要件 4.5.5 の確定文言をそのまま出す。短縮・言い換えをしない。
 *
 * **版と著作権表示は `lib/version.ts` から読む**（ここに書き写さない。要件 8.2）。
 *
 * **並びを入れ替えない**（出典 → 非公式表明 → /about → © → 版）。
 * **© を出典より先に置くと、公式記録そのものへの権利主張と読まれる。**
 */
export function Footer() {
  return (
    <footer className="mt-10 border-t border-rule pt-4 pb-6 text-[12px] leading-relaxed text-ink-3">
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
        <Link
          href="/about/"
          className="flex min-h-11 items-center text-[14px] font-bold text-ink-2 underline"
        >
          免責・出典・個人情報の取扱い
        </Link>
      </nav>
      {/* 著作権表示と版（要件 F-12）。**数字は等幅で組む**（ui-implementation の
          規約）。`font-mono` を直接書かず `num` を使う（基本設計 6.2）。ただし
          ここは主数値ではなく注記の行なので、**ウェイトは上げない**。
          リンクにしない — 遷移先が無い。**© を出典より先に置かない** */}
      <p className="num mt-3 font-normal">{SITE_COPYRIGHT}</p>
      <p className="num font-normal">{SITE_VERSION}</p>
    </footer>
  );
}
