// Route segments owned by the app shell — claiming one as a username would
// make the public page unreachable (static routes win over [username]).
export const RESERVED_USERNAMES = new Set([
  "auth",
  "dashboard",
  "account",
  "api",
  "admin",
  "login",
  "signin",
  "signout",
  "settings",
  "help",
  "about",
  "null",
  "undefined",
]);

export function validUsername(v: string) {
  return /^[a-z0-9_]{3,20}$/.test(v) && !RESERVED_USERNAMES.has(v);
}
