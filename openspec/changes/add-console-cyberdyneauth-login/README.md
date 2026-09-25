# add-console-cyberdyneauth-login

Sign admins into the console with CyberdyneAuth (OIDC, authorization code +
PKCE) through a backend-for-frontend: the admin API does the code exchange and
refresh, and the browser only ever holds an httpOnly session cookie. Adds two
roles from the CyberdyneAuth client roles, `admin` (read/write) and `operator`
(read-only), CSRF protection for cookie auth, and audit attribution by OIDC
subject. The static `cfa_` tokens stay for scripts.

- `proposal.md`: why, and what changes
- `design.md`: the BFF, the verification rules, sessions, CSRF, roles
- `specs/console-authentication/spec.md`: the new requirements
- `specs/admin-console/spec.md`: the modified identity requirement
- `tasks.md`: progress
