# rewrite-console-svelte

Rewrite the admin console bundle (`console/`) from React to Svelte 5 with an
MVVM layout: views, view-models, services. The port is screen for screen. The
admin API, the Docker build path and the URL the console is served from do not
change. New screens (usage, feature requests) come in later changes and slot
into the same layout.

- `proposal.md`: why, and what changes
- `design.md`: stack, layers, and how the existing guards carry over
- `specs/admin-console-ui/spec.md`: the requirements
- `tasks.md`: progress
