export const ping = () => {
  const f = fetch;
  return f("/x");
};
