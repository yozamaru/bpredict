// Flat Config。next lint は Next.js 16 で削除されているため eslint を直接呼ぶ（CLAUDE.md）
import js from '@eslint/js';
import tseslint from 'typescript-eslint';
import reactHooks from 'eslint-plugin-react-hooks';

export default tseslint.config(
  { ignores: ['.next/**', 'out/**', 'next-env.d.ts'] },
  js.configs.recommended,
  tseslint.configs.recommendedTypeChecked,
  {
    languageOptions: {
      parserOptions: { projectService: true, tsconfigRootDir: import.meta.dirname },
    },
  },
  {
    files: ['**/*.tsx'],
    plugins: { 'react-hooks': reactHooks },
    rules: { ...reactHooks.configs.recommended.rules },
  },
  {
    // 設定ファイルと Node スクリプトは型情報つきの検査から外す
    files: ['*.mjs', '*.ts', 'scripts/**/*.mjs'],
    extends: [tseslint.configs.disableTypeChecked],
  },
  {
    // Node で動かすスクリプト。globals パッケージを足さず、使うものだけ宣言する
    files: ['scripts/**/*.mjs'],
    languageOptions: {
      globals: { URL: 'readonly', console: 'readonly', process: 'readonly' },
    },
  },
  {
    // 単体テスト。`node:test` の `test()` は Promise を返すが、**実行器が待つ**
    // （テスト側で await すると、1件ごとに直列化されて意味が変わる）。
    // この1件だけを外す — 型情報つきの検査そのものは残す（除くと静かに型が崩れる）
    files: ['lib/__tests__/**/*.ts'],
    rules: { '@typescript-eslint/no-floating-promises': 'off' },
  },
);
