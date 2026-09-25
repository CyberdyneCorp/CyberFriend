Inputs for the source-scan guards in `src/`. They sit outside `src/` so the
real scans never see them; the guard tests point the same scanner here to
prove it catches (and does not over-catch) each file type the console may be
written in, and each import the layer rule forbids.

`credential-guard/` is for the request-mode scan in `no-browser-storage.test.ts`:
a credential header outside `services/breakGlassHttp.ts`, or a same-origin
cookie request outside `services/http.ts`, must be caught; comments must not.
