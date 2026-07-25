// Flat ESLint config (ESLint 9) for the Vite + React + TypeScript frontend.
// Non-pedantic on purpose: the JS/TS "recommended" sets (real bugs, not style)
// plus the two Vite-convention plugins. Formatting is left to the editor —
// lint should fail on mistakes, not on whitespace.
import js from "@eslint/js";
import globals from "globals";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";

export default tseslint.config(
  // Never lint build output or vendored code.
  { ignores: ["dist", "node_modules"] },
  {
    files: ["**/*.{ts,tsx}"],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      // Fast-refresh boundary hint — warn, don't fail the build over it.
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],
      // The app uses a few deliberate `any`s at fetch/SSE boundaries; flag the
      // riskier bugs (unused vars) as errors but don't gate CI on every `any`.
      "@typescript-eslint/no-explicit-any": "off",
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
    },
  },
);
