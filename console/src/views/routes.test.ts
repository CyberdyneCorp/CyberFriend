/**
 * Each screen's `minRole` is what the server asks of the reads it makes.
 *
 * A higher role here hides a screen from people the server would serve; a
 * lower one shows a screen that loads nothing but refusals. The server's
 * `ROUTE_ACCESS` table is the authority, so this reads it rather than
 * restating it.
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import type { Role } from "../domain/roles";
import { ROUTES } from "./routes";

const SERVER = fileURLToPath(new URL("../../../src/chatmemory/admin/server.py", import.meta.url));

/** The API reads each screen makes when it loads. */
const READS: Record<string, string[]> = {
  "/status": ["/api/status", "/api/audit"],
  "/federation": ["/api/federation/servers", "/api/federation/allowlist"],
  "/channels": ["/api/channels"],
  "/retention": ["/api/settings", "/api/optouts"],
  "/settings": ["/api/settings"],
  "/tokens": ["/api/tokens"],
  "/audit": ["/api/audit"],
};

const ACCESS: Record<string, Role> = { _OPERATOR: "operator", _ADMIN: "admin" };

/** `("GET", "/api/x"): _OPERATOR` rows of the server's table, as path -> role. */
function serverReads(): Map<string, Role> {
  const rows = readFileSync(SERVER, "utf8").matchAll(/\("GET", "(\/api\/[^"]*)"\): (_[A-Z]+)/g);
  return new Map(Array.from(rows, ([, path, access]) => [path ?? "", ACCESS[access ?? ""] ?? "admin"]));
}

describe("the route table", () => {
  const reads = serverReads();

  it("names the reads of every screen, and the server has a row for each", () => {
    expect(Object.keys(READS).sort()).toEqual(ROUTES.map((route) => route.path).sort());
    for (const path of Object.values(READS).flat()) expect(reads.has(path), path).toBe(true);
  });

  it.each(ROUTES.map((route) => [route.path, route.minRole] as const))(
    "asks for %s exactly what its reads need",
    (path, minRole) => {
      const needed = (READS[path] ?? []).map((read) => reads.get(read) ?? "admin");
      const strictest: Role = needed.some((role) => role === "admin") ? "admin" : "operator";
      expect(minRole).toBe(strictest);
    },
  );
});
