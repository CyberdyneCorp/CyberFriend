import type { Status } from "../domain/types";

export const status = (): Promise<Status> => fetch("/api/status").then((r) => r.json());
