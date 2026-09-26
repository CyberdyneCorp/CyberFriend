import { partition } from "../domain/format";
import type { AdminApi } from "../services/adminApi";
import { Resource } from "./resource.svelte";

export const make = (api: AdminApi) => new Resource(() => api.status().then(partition));
