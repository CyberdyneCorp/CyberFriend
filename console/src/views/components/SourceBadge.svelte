<!--
  Where a setting's value came from. Without it, a setting edited here but
  overridden by an environment variable is indistinguishable from one that did
  not save, and the operator's next move differs completely between the two.
-->
<script lang="ts">
  import { sourceOf, type Provenance } from "../../domain/settings";

  let { source }: { source: string } = $props();

  const HELP: Record<Provenance, string> = {
    database: "Set here, and in force.",
    environment:
      "An environment variable is in force. Editing this here will not take effect until that variable is removed.",
    default: "Nothing has set this; the built-in default is in force.",
    unknown:
      "The API reported a provenance this console does not recognise. Treat the value as unexplained rather than as saved.",
  };

  const provenance = $derived(sourceOf(source));
</script>

<span class="badge badge--source-{provenance}" title={HELP[provenance]}>
  {provenance === "unknown" ? `unknown (${source})` : provenance}
</span>
