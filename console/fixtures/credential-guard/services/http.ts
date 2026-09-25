// A cookie-mode client that also sets a credential header: refused.
export const init = { headers: { Authorization: "Bearer x" }, credentials: "same-origin" };
