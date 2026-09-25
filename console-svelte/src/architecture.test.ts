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
 * The domain imports no packages either, only other domain files.
 *
 * Files at the top of `src/` (the composition root `main.ts`) are outside the
 * import rule, because they are where the layers are wired together, but not
 * outside the network rule. `*.test.ts` files and the helpers in `test/` are
 * outside both: they are where the layers are faked.
 *
 * The files are read with the TypeScript parser and the Svelte compiler (see
 * `test/moduleFacts.ts`), not a pattern: a pattern misses every spelling it
 * did not anticipate, and a guard that misses silently is worse than none.
 */

import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { factsOf, type Import } from "./test/moduleFacts";
import { sourceFiles } from "./test/sourceScan";

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

/** Layers that may not import packages either: the domain is plain TypeScript. */
const NO_PACKAGES: readonly Layer[] = ["domain"];

/** The one file that talks to the network. */
const NETWORK_CLIENT = "services/http.ts";

/** Test helpers: fakes and scanners, outside the rule like `*.test.ts`. */
const TEST_HELPERS = "test/";

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

/** Why this one import breaks the rule, or null when it does not. */
function importRefusal(root: string, file: string, from: Layer, { specifier, typeOnly }: Import): string | null {
  if (specifier === null) return `${from} may not import a computed path`;
  if (!specifier.startsWith(".")) {
    return NO_PACKAGES.includes(from) ? `${from} may not import packages` : null;
  }
  const target = layerOf(relative(root, resolve(dirname(join(root, file)), specifier)));
  return target === null ? `${from} may not import outside the layers` : refusal(from, target, typeOnly);
}

function importViolations(root: string, file: string, from: Layer, imports: Import[]): string[] {
  return imports.flatMap((made) => {
    const why = importRefusal(root, file, from, made);
    return why === null ? [] : [`${file} -> ${made.specifier ?? "(computed)"}: ${why}`];
  });
}

/**
 * Every broken rule under `root`, as `file -> import: reason`. The network
 * rule covers every file, the composition root included; the import rule
 * covers the layers.
 */
function violations(root: string): string[] {
  const files = sourceFiles(root, (name) => name.endsWith(".test.ts"))
    .map((path) => relative(root, path))
    .filter((file) => !file.startsWith(TEST_HELPERS));
  return files.flatMap((file) => {
    const facts = factsOf(join(root, file));
    const from = layerOf(file);
    const network =
      file !== NETWORK_CLIENT && facts.network.length > 0
        ? [`${file}: only ${NETWORK_CLIENT} makes network requests`]
        : [];
    return [...(from === null ? [] : importViolations(root, file, from, facts.imports)), ...network];
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
      "domain/usesPackage.ts -> svelte/store: domain may not import packages",
      "main.ts: only services/http.ts makes network requests",
      "services/computedImport.ts -> (computed): services may not import a computed path",
      "services/dynamic.ts -> ../viewmodels/status.svelte: services may not import viewmodels",
      "services/valueFromDomain.ts -> ../domain/format: services may import only types from domain",
      "viewmodels/aliasesFetch.svelte.ts: only services/http.ts makes network requests",
      "viewmodels/computedGlobal.svelte.ts: only services/http.ts makes network requests",
      "viewmodels/usesView.svelte.ts -> ../views/App.svelte: viewmodels may not import views",
      "views/Fetches.svelte: only services/http.ts makes network requests",
      "views/FetchesInMarkup.svelte: only services/http.ts makes network requests",
      "views/Leaky.svelte -> ../services/adminApi: views may not import services",
      "views/NoSpaceBeforeFrom.svelte -> ../services/http: views may not import services",
      "views/SameLineAsScript.svelte -> ../services/http: views may not import services",
      "views/TypeFromService.svelte -> ../services/adminApi: views may not import services",
    ]);
  });

  it("allows what the rule allows, and ignores comments", () => {
    expect(violations(join(FIXTURES, "clean"))).toEqual([]);
  });
});
