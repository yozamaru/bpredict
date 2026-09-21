// ESLint は Flat Config。`.eslintrc.json` を作らない（CLAUDE.md / Next.js 16）。
// `next lint` は削除されているため、CI は `eslint .` を直接呼ぶ。
import js from '@eslint/js';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  {
    ignores: [
      'node_modules/**',
      '.wrangler/**',
      'dist/**',
      // wrangler types の生成物。手で直さないのでリントもしない
      'worker-configuration.d.ts',
    ],
  },
  js.configs.recommended,
  ...tseslint.configs.recommendedTypeChecked,
  {
    languageOptions: {
      parserOptions: { projectService: true, tsconfigRootDir: import.meta.dirname },
    },
    rules: {
      // 例外オブジェクトをそのままログ・レスポンスに入れないため、
      // any による型の穴を塞ぐ（CLAUDE.md 絶対ルール4）。
      '@typescript-eslint/no-explicit-any': 'error',
      '@typescript-eslint/no-unsafe-assignment': 'error',
      eqeqeq: ['error', 'always'],
    },
  },
  {
    files: ['tests/**/*.ts'],
    rules: { '@typescript-eslint/no-unsafe-assignment': 'off' },
  },
  {
    // 設定ファイルは tsconfig の project service の外にあるため、
    // 型情報を要するルールを外す
    files: ['eslint.config.mjs'],
    ...tseslint.configs.disableTypeChecked,
  },
);
