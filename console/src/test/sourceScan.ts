/**
 * Reading the console's own sources, for the tests that guard them.
 *
 * The storage guard and the architecture test are source scans rather than
 * behavioural tests on purpose: what they prevent is a line somebody adds in
 * six months, and no test of today's behaviour would notice it.
 */

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

/** Every file type the console is written in; `.svelte.ts` is covered by `.ts`. */
const SOURCE = /\.(ts|svelte)$/;

/** Source files under `dir`, sorted so a report's order is stable. */
export function sourceFiles(dir: string, skip: (name: string) => boolean = () => false): string[] {
  return readdirSync(dir)
    .sort()
    .flatMap((entry) => {
      const path = join(dir, entry);
      if (statSync(path).isDirectory()) return sourceFiles(path, skip);
      return SOURCE.test(entry) && !skip(entry) ? [path] : [];
    });
}

/**
 * The code, without the prose about it: HTML comments (Svelte markup only --
 * in TypeScript `<!--` is ordinary characters), block comments and whole-line
 * `//` comments. An inline trailing comment is left alone rather than risk
 * truncating the code before it.
 */
export function codeOf(path: string): string {
  const source = readFileSync(path, "utf8");
  const markup = path.endsWith(".svelte") ? source.replace(/<!--[\s\S]*?-->/g, "") : source;
  return markup
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .split("\n")
    .filter((line) => !line.trimStart().startsWith("//"))
    .join("\n");
}
