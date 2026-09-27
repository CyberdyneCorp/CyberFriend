"""The web user area: a person's own data, after signing in with CyberdyneAuth.

It lives in the admin API process, on the same origin, and shares nothing with
the console but the OIDC mechanics (`admin.oidc`):

*   `store` -- the `user_session` record behind the `__Host-cf_user` cookie.
*   `service` -- `/link` (a DM'd code redeemed after sign-in), the plain and
    the fresh (`max_age`) sign-ins, and authenticating a user session.
*   `auth` -- the `/me` middleware. It accepts only the user cookie, and the
    console's middleware only the admin cookie or a bearer, so neither session
    can satisfy the other's routes.
*   `routes` -- `/link`, `/auth/user/*` and `/me/*`.

Every `/me` route finds the person from the session's subject through
`person_account_link`, and from nothing the browser sends.
"""
