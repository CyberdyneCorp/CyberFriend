/**
 * What a console source file imports and which network globals it names,
 * read with the real parsers rather than a pattern over the text.
 *
 * A regex over source text misses whatever it did not anticipate: an import
 * on the same line as `<script>`, `{send}from`, `const f = fetch`. The
 * TypeScript parser and the Svelte compiler already know the grammar, so the
 * layer rule asks them.
 *
 * - Imports come from the TypeScript AST of the file, or of a component's
 *   `<script>` blocks, so `import type` is still visible.
 * - Network use is looked for in the code that will actually run: the
 *   TypeScript itself, or a component compiled to JavaScript, which puts the
 *   markup's expressions (`{#await fetch(...)}`) in the same tree.
 */

import { readFileSync } from "node:fs";

import { compile, parse } from "svelte/compiler";
import ts from "typescript";

export interface Import {
  /** The module named, or null for a dynamic import of a computed path. */
  specifier: string | null;
  typeOnly: boolean;
}

export interface ModuleFacts {
  imports: Import[];
  /** Each place the file names a way to reach the network, e.g. `fetch`. */
  network: string[];
}

/** Globals that reach the network, however they are spelled or aliased. */
const NETWORK_NAMES = new Set(["fetch", "XMLHttpRequest", "WebSocket", "EventSource", "sendBeacon"]);

/** Objects a global can be read off with a computed key, e.g. `globalThis["fe" + "tch"]`. */
const GLOBAL_OBJECTS = new Set(["globalThis", "window", "self"]);

function treeOf(code: string, name: string): ts.SourceFile {
  return ts.createSourceFile(name, code, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
}

function visit(node: ts.Node, each: (node: ts.Node) => void): void {
  each(node);
  ts.forEachChild(node, (child) => visit(child, each));
}

/** True when every named binding is `type`-only and nothing else is bound. */
function allTypeOnly(elements: ts.NodeArray<ts.ImportSpecifier | ts.ExportSpecifier>): boolean {
  return elements.length > 0 && elements.every((element) => element.isTypeOnly);
}

function importTypeOnly(clause: ts.ImportClause | undefined): boolean {
  if (clause === undefined) return false;
  if (clause.isTypeOnly) return true;
  const named = clause.namedBindings;
  return clause.name === undefined && named !== undefined && ts.isNamedImports(named) && allTypeOnly(named.elements);
}

function exportTypeOnly(node: ts.ExportDeclaration): boolean {
  const clause = node.exportClause;
  return node.isTypeOnly || (clause !== undefined && ts.isNamedExports(clause) && allTypeOnly(clause.elements));
}

function literal(node: ts.Node | undefined): string | null {
  return node !== undefined && ts.isStringLiteralLike(node) ? node.text : null;
}

function isDynamicImport(node: ts.Node): node is ts.CallExpression {
  return ts.isCallExpression(node) && node.expression.kind === ts.SyntaxKind.ImportKeyword;
}

/** The import `node` makes, if it makes one. */
function importAt(node: ts.Node): Import | null {
  if (ts.isImportDeclaration(node)) {
    return { specifier: literal(node.moduleSpecifier), typeOnly: importTypeOnly(node.importClause) };
  }
  if (ts.isExportDeclaration(node) && node.moduleSpecifier !== undefined) {
    return { specifier: literal(node.moduleSpecifier), typeOnly: exportTypeOnly(node) };
  }
  if (ts.isImportEqualsDeclaration(node) && ts.isExternalModuleReference(node.moduleReference)) {
    return { specifier: literal(node.moduleReference.expression), typeOnly: node.isTypeOnly };
  }
  if (isDynamicImport(node)) return { specifier: literal(node.arguments[0]), typeOnly: false };
  if (ts.isImportTypeNode(node) && ts.isLiteralTypeNode(node.argument)) {
    return { specifier: literal(node.argument.literal), typeOnly: true };
  }
  return null;
}

function importsIn(tree: ts.SourceFile): Import[] {
  const found: Import[] = [];
  visit(tree, (node) => {
    const made = importAt(node);
    if (made !== null) found.push(made);
  });
  return found;
}

function isComputedGlobal(node: ts.Node): boolean {
  return (
    ts.isElementAccessExpression(node) &&
    ts.isIdentifier(node.expression) &&
    GLOBAL_OBJECTS.has(node.expression.text) &&
    !ts.isStringLiteralLike(node.argumentExpression)
  );
}

/** Why `node` reaches for the network, or null when it does not. */
function networkAt(node: ts.Node): string | null {
  if ((ts.isIdentifier(node) || ts.isStringLiteralLike(node)) && NETWORK_NAMES.has(node.text)) {
    return node.text;
  }
  return isComputedGlobal(node) ? "a global read with a computed key" : null;
}

function networkIn(tree: ts.SourceFile): string[] {
  const found: string[] = [];
  visit(tree, (node) => {
    const why = networkAt(node);
    if (why !== null) found.push(why);
  });
  return found;
}

/** Offsets into the component's source; the parser sets them, estree's types omit them. */
interface Located {
  start: number;
  end: number;
}

/** A component's `<script>` blocks, as the TypeScript they are. */
function scriptsOf(source: string): string {
  const root = parse(source, { modern: true });
  return [root.module, root.instance]
    .flatMap((script) => (script == null ? [] : [script.content as unknown as Located]))
    .map(({ start, end }) => source.slice(start, end))
    .join("\n");
}

function componentFacts(path: string, source: string): ModuleFacts {
  const compiled = compile(source, { filename: path, generate: "client" }).js.code;
  return {
    imports: importsIn(treeOf(scriptsOf(source), path)),
    network: networkIn(treeOf(compiled, path)),
  };
}

export function factsOf(path: string): ModuleFacts {
  const source = readFileSync(path, "utf8");
  if (path.endsWith(".svelte")) return componentFacts(path, source);
  const tree = treeOf(source, path);
  return { imports: importsIn(tree), network: networkIn(tree) };
}
