<script lang="ts">
  let {
    value,
    yes,
    no,
    good = true,
  }: {
    value: boolean;
    yes: string;
    no: string;
    /** Whether `true` is the reassuring answer; a mutation that is enabled is not. */
    good?: boolean;
  } = $props();
</script>

<span class="badge badge--{value === good ? 'ok' : 'warn'}">{value ? yes : no}</span>
