/**
 * The admin token is held in memory only. This is the test that keeps it there.
 *
 * It is a source scan rather than a behavioural test on purpose: the failure
 * being prevented is somebody adding a "remember me" checkbox in six months,
 * and no unit test of today's code would notice that. A console token in
 * browser storage outlives the tab, the session and the attention of whoever
 * pasted it, and this console can widen what the agent may do.
 */

import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { codeOf as code, sourceFiles } from "./test/sourceScan";

const SRC = fileURLToPath(new URL(".", import.meta.url));
const FIXTURES = fileURLToPath(new URL("../fixtures/storage-guard/", import.meta.url));

/**
 * The one module, besides the session module, allowed to read the token: it
 * hands a held break-glass token to `breakGlassHttp.ts` for the request.
 */
const TOKEN_READER = "services/http.ts";
const SESSION_MODULE = "services/session.ts";

/**
 * Where a credential header may be written, and where the browser may be told
 * to attach cookies. Two files, two modes, so a cookie request can never carry
 * a token and a token request can never carry a cookie.
 */
const CREDENTIAL_HEADER_WRITER = "services/breakGlassHttp.ts";
const COOKIE_SENDER = "services/http.ts";
const CREDENTIAL_HEADER = /authorization|bearer/i;
const COOKIE_MODE = "same-origin";

const FORBIDDEN = [
  "localStorage",
  "sessionStorage",
  "indexedDB",
  "document.cookie",
  "window.name",
];

/**
 * `.ts`, `.svelte` and `.svelte.ts` under `dir`, except this file: it names
 * every API it forbids, so it would fail its own scan. Comments are stripped
 * before matching (see `codeOf`), so prose about why the token is not in
 * `localStorage` does not count.
 */
function sources(dir: string): string[] {
  return sourceFiles(dir, (name) => name === "no-browser-storage.test.ts");
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
    // reader is how a token ends up in a log line or a component prop.
    const readers = sources(SRC).filter(
      (path) => !path.endsWith(SESSION_MODULE) && code(path).includes("currentToken"),
    );
    expect(readers.map((p) => p.slice(SRC.length))).toEqual([TOKEN_READER]);
  });
});

/**
 * `relative/path: what` for every source under `root` that writes a credential
 * header or asks for cookies outside the one file allowed to. Tests and their
 * helpers are outside it: they read those headers to assert on them.
 */
function credentialModeUses(root: string): string[] {
  const files = sourceFiles(root, (name) => name.endsWith(".test.ts")).filter(
    (path) => !path.slice(root.length).startsWith("test/"),
  );
  return files.flatMap((path) => {
    const relative = path.slice(root.length);
    const text = code(path);
    const found: string[] = [];
    if (relative !== CREDENTIAL_HEADER_WRITER && CREDENTIAL_HEADER.test(text)) {
      found.push(`${relative}: credential header`);
    }
    if (relative !== COOKIE_SENDER && text.includes(COOKIE_MODE)) found.push(`${relative}: ${COOKIE_MODE}`);
    return found;
  });
}

describe("the two request modes", () => {
  it("writes a credential header only in the break-glass module, and asks for cookies only in http.ts", () => {
    expect(credentialModeUses(SRC)).toEqual([]);
  });

  it("sees both modes where they are allowed, so a green run means something", () => {
    expect(CREDENTIAL_HEADER.test(code(join(SRC, CREDENTIAL_HEADER_WRITER)))).toBe(true);
    expect(code(join(SRC, COOKIE_SENDER))).toContain(COOKIE_MODE);
  });

  it("catches either mode in the wrong file", () => {
    expect(credentialModeUses(join(FIXTURES, "../credential-guard/"))).toEqual([
      "services/http.ts: credential header",
      "views/Screen.svelte: same-origin",
    ]);
  });
});

describe("the storage guard itself", () => {
  it("catches storage use in Svelte markup and in .svelte.ts modules", () => {
    expect(storageUses(join(FIXTURES, "leaks"))).toEqual([
      "/RememberMe.svelte: localStorage",
      "/html-comment-markers.ts: localStorage",
      "/session.svelte.ts: sessionStorage",
    ]);
  });

  it("does not count HTML or script comments as storage use", () => {
    expect(storageUses(join(FIXTURES, "clean"))).toEqual([]);
  });
});
