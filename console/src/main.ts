/**
 * The composition root: the only module that names both services and views.
 * It wires the real services into the view-models and mounts the root view.
 */

import { mount } from "svelte";

import { adminApi } from "./services/adminApi";
import { browserHash } from "./services/hashLocation";
import { session } from "./services/session";
import { sessionApi } from "./services/sessionApi";
import "./styles.css";
import { ConsoleVM } from "./viewmodels/console.svelte";
import App from "./views/App.svelte";

const host = document.getElementById("root");
if (host === null) throw new Error("index.html is missing #root");

mount(App, { target: host, props: { app: new ConsoleVM(adminApi, sessionApi, session, browserHash) } });
