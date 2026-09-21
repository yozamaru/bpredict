import type { Metadata, Viewport } from 'next';
import './globals.css';
import { Footer } from '@/components/ui/Footer';
import { Header } from '@/components/ui/Header';

export const metadata: Metadata = {
  title: 'B.PREDICT（仮称）',
  description:
    'B.LEAGUE PREMIER の試合の勝敗確率・予想スコア・個人スタッツを、判断根拠と的中率とともに公開する非公式ツール。',
};

export const viewport: Viewport = {
  width: 'device-width',
  initialScale: 1,
};

// テーマ適用の inline script。**最初から CSP の sha256 指定で許可する形で書く**
// （詳細設計 3.6）。後から CSP を入れるとこのスクリプトが真っ先に壊れる。
// localStorage へのアクセスは必ず try/catch で囲む（CLAUDE.md UI）。
const THEME_SCRIPT =
  "try { var t = localStorage.getItem('theme');" +
  " if (t) document.documentElement.setAttribute('data-theme', t); } catch (e) {}";

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ja">
      <head>
        {/* dangerouslySetInnerHTML は使わない（CLAUDE.md UI）。
            React 19 は script の子要素に文字列を取れるため、インラインのまま
            書ける。CSP には この文字列の sha256 を登録する（詳細設計 3.6）。 */}
        <script>{THEME_SCRIPT}</script>
      </head>
      <body>
        {/* コンテンツ幅の上限は 440px 程度、左右 16px のガター（基本設計 6.3） */}
        <div className="mx-auto flex min-h-dvh max-w-[440px] flex-col px-4">
          <Header />
          <main className="flex-1 pb-8">{children}</main>
          <Footer />
        </div>
      </body>
    </html>
  );
}
