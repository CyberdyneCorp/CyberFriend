<!-- A screen that says only which role it was opened with, for tests about what surrounds screens. -->
<script lang="ts">
  let { app }: { app: { session: { role: string | null } } } = $props();
</script>

<p>blank screen for {app.session.role}</p>
