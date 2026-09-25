import { describe, expect, it } from "vitest";

import type { HashPort } from "../services/hashLocation";
import { Router, type Route } from "./router.svelte";

function fakeHash(start: string): HashPort & { go: (path: string) => void; replaced: string[] } {
  let path = start;
  const listeners = new Set<() => void>();
  const replaced: string[] = [];
  return {
    replaced,
    read: () => path,
    replace: (next) => {
      replaced.push(next);
      path = next;
    },
    listen: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    go: (next) => {
      path = next;
      listeners.forEach((listener) => listener());
    },
  };
}

const admin = () => "admin" as const;

const ROUTES: Route<string>[] = [
  { path: "/status", title: "Status", view: "status", minRole: "operator" },
  { path: "/tokens", title: "Tokens", view: "tokens", minRole: "operator" },
  { path: "/purge", title: "Purge", view: "purge", minRole: "admin" },
];

describe("Router", () => {
  it("opens the screen the address names", () => {
    const router = new Router(ROUTES, fakeHash("/tokens"), "/status", admin);
    expect(router.current.view).toBe("tokens");
  });

  it("sends an unknown address to the status screen", () => {
    for (const unknown of ["", "/", "/nope", "/status/extra"]) {
      const hash = fakeHash(unknown);
      const router = new Router(ROUTES, hash, "/status", admin);
      expect(router.current.view).toBe("status");
      expect(hash.replaced).toEqual(["/status"]);
    }
  });

  it("follows the address while started, and not after", () => {
    const hash = fakeHash("/status");
    const router = new Router(ROUTES, hash, "/status", admin);
    router.start();
    hash.go("/tokens");
    expect(router.current.view).toBe("tokens");
    hash.go("/elsewhere");
    expect(router.current.view).toBe("status");
    router.stop();
    hash.go("/tokens");
    expect(router.current.view).toBe("status");
  });

  it("offers each role only the routes it may open", () => {
    const router = new Router(ROUTES, fakeHash("/status"), "/status", admin);
    expect(router.visibleTo("operator").map((route) => route.path)).toEqual([
      "/status",
      "/tokens",
    ]);
    expect(router.visibleTo("admin")).toHaveLength(3);
    expect(router.visibleTo(null)).toEqual([]);
  });

  it("refuses a table without the fallback route", () => {
    expect(() => new Router(ROUTES, fakeHash(""), "/home", admin)).toThrow("/home");
  });

  it("will not open a screen above the signed-in role", () => {
    const hash = fakeHash("/purge");
    const router = new Router(ROUTES, hash, "/status", () => "operator");
    expect(router.current.view).toBe("status");
    expect(hash.replaced).toEqual(["/status"]);
    router.start();
    hash.go("/purge");
    expect(router.current.view).toBe("status");
    hash.go("/tokens");
    expect(router.current.view).toBe("tokens");
  });
});
