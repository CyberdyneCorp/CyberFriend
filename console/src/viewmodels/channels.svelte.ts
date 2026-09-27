/**
 * What is indexed, and whether the bot can actually read it.
 *
 * Those are two different facts and the screen keeps them apart. A channel in
 * scope that the bot cannot read produces no error anywhere -- it simply
 * indexes nothing -- and "why is this channel not searchable" is otherwise a
 * question with no answer on any screen.
 */

import { detailOf } from "../domain/changed";
import type { Channel, ChannelAdded } from "../domain/types";
import type { AdminApi } from "../services/adminApi";
import { Action } from "./action.svelte";
import { Resource } from "./resource.svelte";

function nameOf(channel: Pick<Channel, "id" | "name">): string {
  return channel.name || channel.id;
}

/**
 * What adding a channel says. Added and unreadable is the case worth saying
 * out loud: it is accepted configuration that will index nothing, and it
 * produces no error anywhere else.
 */
export function addedNote(added: ChannelAdded): string {
  const said = detailOf(added, `${nameOf(added.channel)} is now in scope`);
  return added.channel.readable_by_bot
    ? said
    : `${said} — but the bot cannot read it, so nothing will be indexed until it is given access.`;
}

export class ChannelsVM {
  readonly channels: Resource<Channel[]>;
  /** The add form's channel id. */
  id = $state("");
  readonly adding = new Action();
  /** Removals, whose outcome shows above the table. */
  readonly action = new Action();
  readonly canAdd = $derived(!this.adding.busy && this.id.trim() !== "");

  readonly #api: Pick<AdminApi, "channels" | "addChannel" | "removeChannel">;

  constructor(api: Pick<AdminApi, "channels" | "addChannel" | "removeChannel">) {
    this.#api = api;
    this.channels = new Resource(() => api.channels());
  }

  load = (): Promise<void> => this.channels.reload();

  add = async (): Promise<void> => {
    if (!this.canAdd) return;
    const id = this.id.trim();
    await this.adding.run(async () => {
      const added = await this.#api.addChannel(id);
      this.id = "";
      void this.load();
      return addedNote(added);
    });
  };

  remove = async (channel: Channel): Promise<void> => {
    await this.action.run(async () => {
      const result = await this.#api.removeChannel(channel.id);
      void this.load();
      return detailOf(result, `${nameOf(channel)} is out of scope.`);
    });
  };
}
