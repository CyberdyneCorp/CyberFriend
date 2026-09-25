<!--
  Loading, refused and empty, told apart.

  "No federated servers" and "we could not ask" render as different things
  here on purpose: on a screen whose job is to show what the agent may reach,
  a failed request that looked like an empty list would be read as a system
  that reaches nothing.
-->
<script lang="ts" generics="T">
  import type { Snippet } from "svelte";

  import type { Resource } from "../../viewmodels/resource.svelte";
  import Problem from "./Problem.svelte";

  let {
    resource,
    empty = "Nothing here.",
    children,
  }: { resource: Resource<T>; empty?: string; children: Snippet<[T]> } = $props();

  const isEmpty = $derived(Array.isArray(resource.data) && resource.data.length === 0);
</script>

{#if resource.error !== null}
  <Problem message={resource.error} onRetry={resource.reload} />
{:else if resource.data === null}
  <p class="muted">{resource.loading ? "Loading…" : "Nothing loaded."}</p>
{:else if isEmpty}
  <p class="muted">{empty}</p>
{:else}
  {@render children(resource.data)}
{/if}
