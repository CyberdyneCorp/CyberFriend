/**
 * The user area's API, one call per route.
 *
 * Separate from `adminApi.ts` and never importing it: the user area has its
 * own session, its own cookie and its own routes (`/me`), and no call here
 * can reach a console route. Signing in is a navigation to `auth/user/login`
 * (or `auth/user/fresh` before deleting everything), which the API finishes
 * with an httpOnly cookie.
 */

import type { Erased, ErasureMode, MePrivacy, MeSession, MySuggestion, Submitted } from "../domain/me";
import { ApiError, pagePath, userRequest } from "./http";

/** Who the user cookie names, or null when there is no user session. */
async function session(): Promise<MeSession | null> {
  try {
    return await userRequest<MeSession>("GET", pagePath("me/session"));
  } catch (cause) {
    if (cause instanceof ApiError && cause.status === 401) return null;
    throw cause;
  }
}

export const userApi = {
  session,
  privacy: () => userRequest<MePrivacy>("GET", pagePath("me/privacy")),
  suggestions: () => userRequest<MySuggestion[]>("GET", pagePath("me/feature-requests")),
  suggest: (text: string) => userRequest<Submitted>("POST", pagePath("me/feature-requests"), { text }),
  erase: (mode: ErasureMode, confirm: string) =>
    userRequest<Erased>("POST", pagePath("me/erase"), { mode, confirm }),
  logout: () => userRequest<{ signed_out: boolean }>("POST", pagePath("me/logout")),
  loginUrl: (): string => pagePath("auth/user/login"),
  freshUrl: (): string => pagePath("auth/user/fresh"),
  /** Leave the page for `url`: the sign-in, which comes back with a cookie. */
  leave: (url: string): void => {
    window.location.assign(url);
  },
};

/** What a view-model is handed: the real one, or a fake with the same shape. */
export type UserApi = typeof userApi;
