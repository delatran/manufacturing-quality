# Cloudflare runtime

The existing `tran-thien-nhan-portfolio` Worker forwards
`https://tranthiennhan.com/manufacturing-quality` and its descendants to the private
`manufacturing-quality` service binding. This Worker has no public `workers.dev`
endpoint or independent custom domain. The portfolio proxy preserves the public
origin and removes the application path prefix before forwarding.

Static frontend files are served from the `ASSETS` binding. Each visitor receives
an opaque, secure, HTTP-only cookie. Dashboard APIs and Agent sessions use the same
visitor Container so immutable analysis contexts remain available during handoff.
Different visitors use different Containers and persistent Durable Object storage.

The Container has no provider credential and no unrestricted Internet access.
Its private outbound handlers provide:

- The exact public case-study JSON source over HTTPS.
- `http://state.internal/snapshot`, a 32 MiB compressed runtime snapshot with ETag
  compare-and-swap writes. SQLite stores the archive in 64 KiB chunks inside one
  transaction. State survives Container sleep, replacement and Durable Object
  eviction.
- The private managed Agents API connection: session creation and retrieval,
  streaming events, input messages, function results, cancellation, saved items
  and turn inspection. Provider session ownership is durable and scoped to one
  visitor. Idempotency keys bind repeated logical submissions to the same payload.
  Only `gpt-6-luna` is allowed. Credentials remain in the Worker secret binding;
  arbitrary provider paths and external destinations are rejected.
- A private liveness request while work is active, so a disconnected browser does
  not make a long-running analytical tool appear idle.

The public boundary requires the exact site origin for writes and WebSockets,
blocks operations-management endpoints, strips visitor credentials before
forwarding, validates request sizes, and applies durable infrastructure admission.
Historical usage and unresolved accounting remain intact. IP admission bounds new
cookie creation, and visitor capacity controls remain separate from execution
limits. Visitors see a retry message when the service is busy and do not need to
pay or log in.

Registered visitors can still read session state and request cancellation when
their execution lease expires and capacity is full. New sessions, prompts and
WebSocket connections continue to require capacity, because the WebSocket also
carries new prompts. Identifier cleanup keeps its scheduled alarm during traffic.

The Container selects `agent_backend="agents_api"` and `unbounded_runs=true`, using
maximum reasoning effort. Application cost, output-token, turn, tool-call and run
duration ceilings are absent. New managed sessions omit `spend_control`; public
configuration represents absent application limits as JSON `null`. The hosted
bridge has no Responses API fallback.

The nonsecret `OPENAI_FAST_MODE="true"` variable in `wrangler.jsonc` is forwarded
to each Container. `Settings.from_env()` maps it to the existing Fast speed
setting, so newly created managed sessions request `agent.service_tier="fast"`.
Set the variable to `"false"` to request Standard processing. Local `.env` files
are excluded from the image and do not configure the deployed Worker. Restart
warm Containers after an environment-only change; existing managed sessions keep
their original settings. `/api/config` exposes the requested `service_tier` for
readback, not proof of the provider's effective tier. [Fast mode](https://developers.openai.com/api/docs/guides/fast-mode)
keeps maximum reasoning effort and [costs twice Luna's applicable Standard token
rates](https://developers.openai.com/api/docs/models/gpt-6-luna). Source changes
take effect on Cloudflare only after deployment.

The normal Agent exposes typed local analytical functions and separate semantic
QA. Its managed environment is `none`; native provider subagents, arbitrary shell
execution and unconfigured hosted tools remain disabled. Provider stop signals,
owner cancellation, model capacity and account limits still apply.

Provider creation intent, session identity and recovery evidence are checkpointed.
On process restart, interrupted managed work is reconciled and cancelled against
its original provider session without replaying tools or starting replacement
generation. Ambiguous outcomes block unsafe continuation. Idle Containers may
sleep; active work sends private liveness requests. Snapshot infrastructure limits
remain 32 MiB compressed, 256 MiB expanded and 10,000 files. These are storage
constraints, not spending or generation ceilings.

## Verification

From this directory:

```powershell
npm ci
npm run check
```

The check runs the edge tests, production frontend build and Worker compilation.
It deliberately skips Container image preparation and does not establish live
Container readiness. Before deployment, build and test the root Dockerfile, then
deploy the actual configured image. Set `WRANGLER_DOCKER_BIN` to the installed
Docker executable when it is absent from `PATH`.

Deployment additionally requires a Cloudflare account eligible for Containers.
Store `OPENAI_API_KEY` as a Worker secret using Wrangler's secret input mechanism
without printing it, passing a key in command arguments, or including `.env` in
the image. `OPENAI_MODEL` is a non-secret text variable in `wrangler.jsonc`:
`OPENAI_MODEL=gpt-6-luna`. Keep it visible as a Text variable in the Cloudflare
dashboard. When migrating an existing deployment, replace its `OPENAI_MODEL`
secret binding with a plain-text binding of the same name and value. Both settings
are scoped to this application. Deploying this Worker alone does not update the
portfolio service binding.

After deployment, read back the active version, full source identity and resolved
image digest. Verify the public application, managed/unbounded configuration, two
independent visitors, Dashboard-to-Agent handoff, real function execution and
numeric evidence, native in-flight steering, cancellation and session recovery.
Local checks alone do not establish production behavior. Delete obsolete versions
and image tags only after the new release passes. Existing portfolio routes require
their own production checks.
