// 免責・出典・個人情報の取扱い・連絡先（要件 F-12 / F-13 / 4.5.4）。
// 責任限定条項は全部免除にしない。消費者契約法8条1項により条項ごと無効になる。
export const dynamic = 'force-static';

export const metadata = { title: 'このサイトについて | B.PREDICT（仮称）' };

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mt-6">
      <h3 className="text-[15px] font-extrabold">{title}</h3>
      <div className="mt-1.5 space-y-2 text-[13px] leading-relaxed text-text-2">{children}</div>
    </section>
  );
}

export default function Page() {
  return (
    <>
      <h2 className="mt-5 text-[19px] font-extrabold">このサイトについて</h2>

      <Section title="非公式であること">
        <p>
          本サイトは、公益社団法人ジャパン・プロフェッショナル・バスケットボールリーグ（B.LEAGUE）
          および各クラブと一切関係がない、個人が運営する非公式サービスです。
        </p>
      </Section>

      <Section title="データの出典">
        <p>
          B.LEAGUE 公式サイトで公表されている試合日程・記録を基に、本サイトが独自に集計・加工しています。
          記事本文・画像・ロゴは使用していません。
        </p>
        <p>
          データは自動で取得しており、取得・変換の過程で誤り・欠落・遅延が生じることがあります。
          正確な情報は公式発表を参照してください。
        </p>
      </Section>

      <Section title="予測について">
        <p>予測は統計モデルによる推定値であり、的中を保証するものではありません。</p>
        <p>
          選手個人のスタッツ予測は、選手個人の能力や将来の評価を示すものではなく、
          過去の統計から算出された機械的な推定値です。
        </p>
        <p>賭博・ギャンブルへの利用を想定しておらず、これを推奨・助長しません。</p>
      </Section>

      <Section title="免責">
        <p>
          本サイトの予測は統計的推定であり、その正確性・完全性・有用性を保証するものではありません。
          本サイトの利用により利用者に生じた損害について、当方は責任を負いません。
          ただし、当方の故意または重大な過失による場合は、この限りではありません。
        </p>
        <p>予告なくサービスを変更・中断・終了することがあります。</p>
        <p>準拠法は日本法とし、管轄は運営者住所地を管轄する地方裁判所とします。</p>
      </Section>

      <Section title="個人情報の取扱い">
        <p>
          本サイトは利用者の個人情報を取得しません（登録・問い合わせフォーム・ログイン機能を持ちません）。
        </p>
        <p>
          選手の氏名等は、試合記録の集計と予測の表示という目的のために利用します。
          生年月日・出身地・契約情報などの、予測に不要な情報は取得しません。
        </p>
        <p>開示・訂正・利用停止等のご請求は、下記の連絡先で受け付けます。</p>
      </Section>

      <Section title="連絡先">
        {/* 公開前に確定させる。仮の連絡先を書かない */}
        <p>準備中です。公開までに記載します。</p>
      </Section>
    </>
  );
}
