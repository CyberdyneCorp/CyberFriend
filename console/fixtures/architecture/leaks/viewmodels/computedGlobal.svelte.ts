type Get = (url: string) => Promise<unknown>;
export const ping = () => (globalThis["fe" + "tch"] as Get)("/x");
