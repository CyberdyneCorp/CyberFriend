// Lint for the console. The rule that matters most here is the cognitive
// complexity limit: the frontend target is 8-12 per function, and a limit that
// only lives in a document is a limit nobody meets.
import js from "@eslint/js";
import sonarjs from "eslint-plugin-sonarjs";
import svelte from "eslint-plugin-svelte";
import globals from "globals";
import ts from "typescript-eslint";

import svelteConfig from "./svelte.config.js";

export default ts.config(
  { ignores: ["dist/", "node_modules/", "fixtures/"] },
  js.configs.recommended,
  ...ts.configs.recommended,
  ...svelte.configs.recommended,
  {
    languageOptions: { globals: { ...globals.browser, ...globals.node } },
    plugins: { sonarjs },
    rules: {
      "sonarjs/cognitive-complexity": ["error", 12],
    },
  },
  {
    files: ["**/*.svelte", "**/*.svelte.ts"],
    languageOptions: {
      parserOptions: {
        projectService: true,
        extraFileExtensions: [".svelte"],
        parser: ts.parser,
        svelteConfig,
      },
    },
  },
);
