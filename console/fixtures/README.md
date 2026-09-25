Inputs for the source-scan guards in `src/`. They sit outside `src/` so the
real scans never see them; the guard tests point the same scanner here to
prove it catches (and does not over-catch) each file type the console may be
written in.
