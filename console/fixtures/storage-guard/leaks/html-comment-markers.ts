// `<!--` and `-->` are not comments in TypeScript: the call between them is code.
const open = "<!--";
localStorage.setItem("token", open);
const close = "-->";
export { close };
