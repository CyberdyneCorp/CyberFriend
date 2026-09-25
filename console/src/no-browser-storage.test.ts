/**
 * The admin token is held in memory only. This is the test that keeps it there.
 *
 * It is a source scan rather than a behavioural test on purpose: the failure
 * being prevented is somebody adding a "remember me" checkbox in six months,
 * and no unit test of today's code would notice that. A console token in
 * browser storage outlives the tab, the session and the attention of whoever
 * pasted it, and this console can widen what the agent may do.
 */

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const SRC = fileURLToPath(new URL(".", import.meta.url));
const FIXTURES = fileURLToPath(new URL("../fixtures/storage-guard/", import.meta.url));

/**
 * The one module, besides the session module, allowed to read the token.
 * One constant so a move (to `services/http.ts`, say) changes the location
 * without loosening the rule.
 */
const TOKEN_READER = "api/client.ts";

const FORBIDDEN = [
  "localStorage",
  "sessionStorage",
  "indexedDB",
  "document.cookie",
  "window.name",
];

/**
 * Every file type the console may be written in: `.ts` and `.tsx` today,
 * `.svelte` and `.svelte.ts` (covered by `.ts`) after the port. A guard that
 * did not know about a file type would pass while checking nothing.
 */
const SOURCE = /\.(ts|tsx|svelte)$/;

function sources(dir: string): string[] {
  // Sorted, so a report's order does not depend on the filesystem.
  return readdirSync(dir).sort().flatMap((entry) => {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) return sources(path);
    // This file names every API it forbids, so it would fail its own scan.
    if (entry === "no-browser-storage.test.ts") return [];
    return SOURCE.test(entry) ? [path] : [];
  });
}

/**
 * The code, without the prose about it.
 *
 * Several files explain *why* the token is not in `localStorage`, and a scan
 * that read those comments as violations would be one nobody could keep
 * green. HTML comments (Svelte markup), block comments and whole-line `//`
 * comments go; an inline trailing comment is left alone rather than risk
 * truncating the code before it.
 */
function code(path: string): string {
  return readFileSync(path, "utf8")
    .replace(/<!--[\s\S]*?-->/g, "")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .split("\n")
    .filter((line) => !line.trimStart().startsWith("//"))
    .join("\n");
}

/** `relative/path: api` for every forbidden storage API used under `root`. */
function storageUses(root: string): string[] {
  return sources(root).flatMap((path) => {
    const text = code(path);
    return FORBIDDEN.filter((api) => text.includes(api)).map(
      (api) => `${path.slice(root.length)}: ${api}`,
    );
  });
}

describe("credential handling", () => {
  it("never persists anything to browser storage", () => {
    expect(storageUses(SRC)).toEqual([]);
  });

  it("keeps the credential out of URLs", () => {
    // A token in a query string lands in proxy logs and in the browser's
    // history, which is browser storage by another name.
    const offenders = sources(SRC).filter((path) =>
      /[?&](token|access_token|api_key)=/.test(code(path)),
    );
    expect(offenders).toEqual([]);
  });

  it("reads the credential in exactly one place outside the session module", () => {
    // `currentToken` feeding the fetch client is the whole surface. A second
    // reader is how a token ends up in a log line or a React prop.
    const readers = sources(SRC).filter(
      (path) => !path.endsWith("auth/session.ts") && code(path).includes("currentToken"),
    );
    expect(readers.map((p) => p.slice(SRC.length))).toEqual([TOKEN_READER]);
  });
});

describe("the storage guard itself", () => {
  it("catches storage use in Svelte markup and in .svelte.ts modules", () => {
    expect(storageUses(join(FIXTURES, "leaks"))).toEqual([
      "/RememberMe.svelte: localStorage",
      "/session.svelte.ts: sessionStorage",
    ]);
  });

  it("does not count HTML or script comments as storage use", () => {
    expect(storageUses(join(FIXTURES, "clean"))).toEqual([]);
  });
});
