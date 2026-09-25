/**
 * The layer rule, enforced.
 *
 *   views/       -> viewmodels/, domain/
 *   viewmodels/  -> services/, domain/
 *   services/    -> domain/ (types only)
 *   domain/      -> nothing
 *
 * and only `services/http.ts` makes a network request. A view that fetched,
 * or imported a service, would put state and I/O back in a component where no
 * node test can reach it, which is what the view-models exist to prevent.
 *
 * Files at the top of `src/` (the composition root `main.ts`, the guards) and
 * `*.test.ts` files are outside the rule: they are where the layers are wired
 * together or faked.
 */

import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { codeOf, sourceFiles } from "./test/sourceScan";

const SRC = fileURLToPath(new URL(".", import.meta.url));
const FIXTURES = fileURLToPath(new URL("../fixtures/architecture/", import.meta.url));

type Layer = "domain" | "services" | "viewmodels" | "views";

/** What each layer may import, besides itself. */
const MAY_IMPORT: Record<Layer, readonly Layer[]> = {
  domain: [],
  services: ["domain"],
  viewmodels: ["services", "domain"],
  views: ["viewmodels", "domain"],
};

/** Imports a layer may make only with `import type`. */
const TYPES_ONLY: Partial<Record<Layer, readonly Layer[]>> = { services: ["domain"] };

/** The one file that talks to the network. */
const NETWORK_CLIENT = "services/http.ts";

const NETWORK = /\b(fetch\s*\(|XMLHttpRequest|WebSocket|EventSource|sendBeacon)/;

interface Import {
  specifier: string;
  typeOnly: boolean;
}

/** Static imports, re-exports and dynamic imports, multi-line ones included. */
function importsOf(code: string): Import[] {
  const found: Import[] = [];
  const statement = /(?:^|[;\n])\s*(?:import|export)\s+(type\s+)?(?:[^"';]*?\sfrom\s*)?["']([^"']+)["']/g;
  for (const match of code.matchAll(statement)) {
    found.push({ specifier: match[2] ?? "", typeOnly: match[1] !== undefined });
  }
  for (const match of code.matchAll(/\bimport\(\s*["']([^"']+)["']\s*\)/g)) {
    found.push({ specifier: match[1] ?? "", typeOnly: false });
  }
  return found;
}

function layerOf(path: string): Layer | null {
  const top = path.split("/")[0];
  return top !== undefined && top in MAY_IMPORT ? (top as Layer) : null;
}

/** Why `from` may not import `target`, or null when it may. */
function refusal(from: Layer, target: Layer, typeOnly: boolean): string | null {
  if (target === from) return null;
  if (!MAY_IMPORT[from].includes(target)) return `${from} may not import ${target}`;
  if (TYPES_ONLY[from]?.includes(target) && !typeOnly) return `${from} may import only types from ${target}`;
  return null;
}

function importViolations(root: string, file: string, from: Layer): string[] {
  return importsOf(codeOf(join(root, file))).flatMap(({ specifier, typeOnly }) => {
    if (!specifier.startsWith(".")) return [];
    const target = layerOf(relative(root, resolve(dirname(join(root, file)), specifier)));
    const why = target === null ? `${from} may not import outside the layers` : refusal(from, target, typeOnly);
    return why === null ? [] : [`${file} -> ${specifier}: ${why}`];
  });
}

/** Every broken rule under `root`, as `file -> import: reason`. */
function violations(root: string): string[] {
  const files = sourceFiles(root, (name) => name.endsWith(".test.ts")).map((path) =>
    relative(root, path),
  );
  return files.flatMap((file) => {
    const from = layerOf(file);
    if (from === null) return [];
    const network =
      file !== NETWORK_CLIENT && NETWORK.test(codeOf(join(root, file)))
        ? [`${file}: only ${NETWORK_CLIENT} makes network requests`]
        : [];
    return [...importViolations(root, file, from), ...network];
  });
}

describe("the layer rule", () => {
  it("holds for the console", () => {
    expect(violations(SRC)).toEqual([]);
  });

  it("sees files in every layer, so a green run means something", () => {
    const layers = new Set(
      sourceFiles(SRC).map((path) => layerOf(relative(SRC, path))),
    );
    expect([...layers].filter((layer) => layer !== null).sort()).toEqual([
      "domain",
      "services",
      "viewmodels",
      "views",
    ]);
  });
});

describe("the layer rule itself", () => {
  it("catches every import and request the rule forbids", () => {
    expect(violations(join(FIXTURES, "leaks"))).toEqual([
      "domain/impure.ts -> ../services/http: domain may not import services",
      "services/dynamic.ts -> ../viewmodels/status.svelte: services may not import viewmodels",
      "services/valueFromDomain.ts -> ../domain/format: services may import only types from domain",
      "viewmodels/usesView.svelte.ts -> ../views/App.svelte: viewmodels may not import views",
      "views/Fetches.svelte: only services/http.ts makes network requests",
      "views/Leaky.svelte -> ../services/adminApi: views may not import services",
    ]);
  });

  it("allows what the rule allows, and ignores comments", () => {
    expect(violations(join(FIXTURES, "clean"))).toEqual([]);
  });
});
