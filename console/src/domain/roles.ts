/**
 * Console roles, in order. `admin` implies `operator`.
 *
 * The server decides who holds which role and refuses what a role may not do;
 * the console uses this only to decide what to show. A screen hidden here is
 * a courtesy, never the protection.
 */

export type Role = "operator" | "admin";

const RANK: Record<Role, number> = { operator: 1, admin: 2 };

/** Whether `held` (null: no console role) is enough for `needed`. */
export function allows(held: Role | null, needed: Role): boolean {
  return held !== null && RANK[held] >= RANK[needed];
}

/**
 * The highest console role among what the API reported, or null for none.
 *
 * Anything the console does not recognise counts for nothing: an unknown
 * role name is not read as a lower or a higher one.
 */
export function roleOf(roles: readonly string[]): Role | null {
  if (roles.includes("admin")) return "admin";
  if (roles.includes("operator")) return "operator";
  return null;
}
