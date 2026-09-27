// The user area's API importing the console's session: refused.
import { signOut } from "./session";
import { send } from "./http";

export const leak = [signOut, send];
