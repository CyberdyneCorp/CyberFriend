import { type Status } from "../domain/types";

export const healthy = (status: Status): boolean => status !== null;
