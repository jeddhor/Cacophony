# Cacophony Studio

The schema design environment (design document sections 45–56).

React, TypeScript, Vite, TanStack Query, Zustand and React Flow — the stack
section 40 recommends.

## Development

The Studio has no backend of its own. Run Cacophony's API, then the Vite dev
server, which proxies `/api` and the WebSocket to it:

```bash
cacophony serve                 # terminal one, on :8765
npm install && npm run dev      # terminal two, on :5173
```

Point at a different backend with `CACOPHONY_API=http://host:port npm run dev`.

## Building

```bash
npm run build
```

Emits into `backend/cacophony/api/static/`, where `cacophony serve` finds and
mounts it. One process, one origin, no CORS. The build directory is generated,
so it is not committed.

## Checks

```bash
npm test          # vitest
npm run typecheck # tsc --noEmit, strict
```

## Layout

```
src/
├── api/        the HTTP client, its types, and the query hooks
├── state/      Zustand: what the server does not know
├── components/ the shell, and the shared presentation pieces
├── pages/      one per destination in section 46's navigation
├── studio/     the field editor and the preview table
├── graphs/     the relationship graph (React Flow)
└── styles/     section 45's visual identity
```

Server state lives in TanStack Query and client state in Zustand, so there is
exactly one copy of anything the backend owns.

## Two things worth knowing

**Colour is never the only signal.** Every generator family has a hue *and* a
word, and every run state has a colour *and* its name. Section 45 asks for a
strong visual identity; an interface that only works for people who can
distinguish violet from magenta is not one.

**Schema edits are patches, not rewrites.** The field editor sends targeted
operations, which the backend applies to the YAML document in place. A form
that saved by re-serialising its own model would strip every comment out of a
documented schema the first time anyone touched it.
