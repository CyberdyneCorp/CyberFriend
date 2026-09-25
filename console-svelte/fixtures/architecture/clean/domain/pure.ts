import type { JsonValue } from "./types";
import { humanise } from "./format";

export const label = (value: JsonValue): string => humanise(String(value));
