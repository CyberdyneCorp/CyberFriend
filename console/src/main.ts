/**
 * The composition root: the only module that names both services and views.
 * It wires the real services into the view-models and mounts the root view.
 *
 * Two applications share this bundle and nothing else: the console, and at
 * `#/me` the user area, each with its own session and API. Which one mounts
 * is decided once, from the address the page was opened at.
 */

import { mount } from "svelte";

import { isUserArea } from "./domain/me";
import { adminApi } from "./services/adminApi";
import { browserHash } from "./services/hashLocation";
import { session } from "./services/session";
import { sessionApi } from "./services/sessionApi";
import { userApi } from "./services/userApi";
import "./styles.css";
import { ConsoleVM } from "./viewmodels/console.svelte";
import { UserAreaVM } from "./viewmodels/me/userArea.svelte";
import App from "./views/App.svelte";
import MeApp from "./views/me/MeApp.svelte";

const host = document.getElementById("root");
if (host === null) throw new Error("index.html is missing #root");

if (isUserArea(browserHash.read())) {
  mount(MeApp, { target: host, props: { area: new UserAreaVM(userApi) } });
} else {
  mount(App, { target: host, props: { app: new ConsoleVM(adminApi, sessionApi, session, browserHash) } });
}
