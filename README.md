# Conductor

A local, MIT-licensed analysis and coordination layer for Pi agents in cmux.
**Conductor analyzes and manages Pi. Codex supplies upstream orchestration and review.
Pi executes operational work.** Conductor runs its own OpenRouter model and exchanges
context with your existing Codex desktop chat. Python 3.11+, macOS 14+, an OpenRouter
key, the Codex CLI/desktop, and Pi support the full desktop workflow.

## Run limits

New runs have no turn, provider-call, or worker-task ceiling (`0` means unlimited).
CLI users can set positive `--max-turns`, `--max-calls`, and `--max-tasks` limits.
Existing runs retain stored limits until explicitly changed. An optional
`--max-spend` stops new requests after recorded OpenRouter spend reaches that USD
threshold; it is postpaid, so the final call can exceed it. Omit it for unlimited
spend; `--max-spend 0` blocks requests. Pi's own provider budget remains separate.

## Desktop app

```sh
./scripts/build-app.sh
mkdir -p "$HOME/Applications"
ditto native/build/Conductor.app "$HOME/Applications/Conductor.app"
python3 scripts/install-pi-observer.py
open "$HOME/Applications/Conductor.app"
```

Bind the relay to your exact existing Codex thread in
`~/.local/share/conductor/swarm/codex-target.json`. It contains `thread_id`, the
absolute Codex CLI `binary` path, and the absolute `coordinator_command` path to
`scripts/conductor-pi-mcp`. The configured thread is the destination; no fallback
thread is created. Select Conductor's model using the searchable OpenRouter dropdown
or `/model`; the model in your Codex desktop chat remains independent.

Register the coordinator tools with Codex once (use the absolute script path):

```sh
codex mcp add conductor-pi -- /absolute/path/to/conductor/scripts/conductor-pi-mcp
```

Conductor's composer runs messages through its own analysis model. It reads Pi
context, dispatches managed tasks, reviews evidence, and reports outcomes to the
pinned Codex chat. The desktop uses the OpenRouter coordinator with a restricted
Pi-only tool catalog; the optional native app-server engine is not used in this
mode because its built-in tools cannot provide the same execution boundary.

Codex uses `conductor_send` with a stable `request_id`, a `message`, and optionally
an existing `chat_id` to send goals or steering to Conductor. `conductor_status`
lists recent chats; `conductor_result` reads delivery and reported results.
The durable inbox waits for the app and model key, then wakes the same manager.
Codex messages are agent feedback with their own durable cursor, not new human
instructions. The original delegated goal stays pinned separately; resolved feedback
can leave the working prompt via `resolve_feedback` with evidence while raw history
remains searchable. HTTP retries do not duplicate feedback. Results use the handled instruction
cursor so an older response cannot complete a newer request. Reports survive an
offline bridge and uncertain external delivery is never blindly replayed.
Closing Conductor stops its manager and inbox processing, while existing Pi and
Codex work continues. Reopen it to process queued requests. The explicit direct
Codex relay and `conductor_reply` remain available for older relay requests.
If newly registered MCP tools are not loaded in the current Codex turn, the same
script supports `--call TOOL` with JSON arguments on stdin.

### Durable swarm coordination

Conductor owns dispatch, dependencies, steering, and collection; Codex sends goals
and reviews the handoff. Follow-up messages preserve unresolved assignments instead
of creating a new task scope. Legacy assignments hidden by an old scope remain
visible until accepted or explicitly superseded. A failed manager does not stop
independent Pi processes: restarting reconciles the original task and owner without
resending accepted or uncertain deliveries.

Pi owners must update their exact `swarm_task` to `completed` with a nonempty result
and verification references. Findings and chat replies alone do not release a task.
Conductor reconciles that submission into `review`, then uses `review_task` to accept
or reject it. Only acceptance releases dependent tasks. A corrective `dispatch_pi`
can specify `supersedes` after the prior Pi task has a terminal report and its local
assignment is unsuccessful (or it was never sent because a dependency failed); live
or uncertain work cannot be silently replaced.

`status` and `wait` return a progress cursor. `wait` suspends model inference until
relevant task results, peer messages/findings, human input, agent feedback, or
cancellation. The harness checks durable state in bounded chunks (60 seconds by
default); chunk timeouts do not call the model. Optional `review_after_seconds`
sets a review deadline. Suspension survives a manager restart, and observer
heartbeats do not wake it. Reuse `after_cursor` to catch changes between calls.
Repeated identical tool failures block with an actionable error; repeated unchanged
inspection suspends when workers are active. Existing explicit ceilings still apply.

Manager `codex_send` accepts `notification_kind` (`progress`, `failure`,
`escalation`, or `final`), `task_key`, `checkpoint`, and `progress_cursor`. Progress
requires a task key and checkpoint. Exact repeats and retries of the same checkpoint
are deduplicated durably; changed checkpoint content needs a new cursor/checkpoint.
New failure, escalation, and final reports remain separate. A deduplicated receipt
identifies the original request for `codex_status`; queued delivery is never upstream
acceptance. Unknown sends are not replayed, while a confirmed pre-enqueue rate limit
can retry the same request. Semantic rephrasings with new identities still require
manager discipline; this is not a natural-language duplicate detector.

Chat lists show the task rather than the Codex transport envelope, recover old
truncated titles from original instructions, and show searchable latest-request
previews. Original transcripts and explicitly chosen titles are preserved.
The chat shows manager spend, unknown charges, waiting, review and blocked counts,
last meaningful progress, and compaction. Settings includes the packaged build's
revision and source fingerprint so an old installed app is distinguishable from the
checkout. Building a new app does not install it.

### Pi native context compaction

The scheduler watches context usage for its assigned sessions and requests native
compaction at a safe idle boundary above 200,000 tokens by default. Configure new
chats in Settings or use `--pi-compaction-tokens`; `0` disables this policy. Only
fresh, exact live instances owned by an accepted assignment are eligible.
The coordinator can also request
`pi_agents` with `action: "compact"`, an observed `session_id` and `instance_id`,
and a stable `request_id` when the worker reports pressure. This uses Pi's native `ctx.compact` API, not a
`/compact` chat message or a replacement summary authored by Conductor.

Busy requests wait for an idle boundary with no tools or pending messages. Urgent
steering can still arrive while compaction is queued. Preservation instructions
cover task constraints, paths, evidence, ownership, and pending handoffs. The public
`compaction_control` records queued/running/completed/failed; a delivery receipt
alone does not prove compaction completed. A five-minute cooldown and leaf checks
prevent immediate repeats. Interrupted requests are not silently replayed after an
observer reload. Pi runtimes without the native API reject the request explicitly.
Install the updated observer using the existing installer; automatic reload waits
for a safe idle point. Updating this repository alone does not update a live Pi
runtime or compact any existing session.

For long active turns, Pi's own automatic compaction must also be configured early
enough. `scripts/configure-pi-compaction.py --help` documents a native Pi settings
helper: supply the exact provider/model and its verified context-window size,
then use `--target-tokens 200000 --dry-run` to preview the change. Remove `--dry-run`
to write only that model's `compaction.modelOverrides` reserve and recent-token
settings atomically. It preserves unrelated settings and an explicitly disabled
global compaction policy. Existing Pi processes must reload their native settings;
the helper does not restart or steer them.

### Execution ownership

Only **data generation** must route through Conductor. Platform operations
(including v2), code changes, inspection, configuration, and verification can run
directly in Codex. Ordinary data writes do not automatically count as generation.

For an explicit bypass, call `conductor_bypass({"operation":"platform"})`.
Operations are `platform`, `code`, `other`, or `data_generation`; the last still
returns the Conductor route. The tool returns a routing decision without running
anything, spending model calls, or changing other chats. Existing permissions and
approval requirements remain in force. Reconcile in-flight delegated work before
taking it over directly. MCP initialization advertises this policy to Codex chats.

For work explicitly delegated to Conductor, operational **data writes** and **environment-related work** go to Pi,
including environment inspection and verification. Codex discovers sessions with
`pi_sessions`, reads their public context with `pi_context`, and delegates with
`pi_dispatch` using an exact session/instance, explicit ownership, and acceptance
criteria. Busy agents receive follow-ups without replacing their current task.
For steering an existing routed assignment, use `pi_control` with its original
`request_id`, a unique `control_id`, and `steer` or `follow_up`; this preserves the
owner and acceptance contract without creating another task. Repeating an unknown
delivery does not resend it: reconcile the owner's state before taking further action.
Conductor exposes no local shell/data/environment execution tool and never falls
back to local execution when a Pi attachment is missing.

Pi updates its assigned `swarm_task`; the local result relay queues terminal
reports back to Codex once. Codex reads `pi_result`, reviews evidence, and records
acceptance with `pi_review`. A Pi completion report alone is not accepted work.
The result relay runs while Conductor is open and catches up after restart.
For work delegated through `conductor_send`, Conductor owns Pi dispatch and review;
Codex should not independently dispatch the same work a second time.

`AGENTS.md` records this execution policy for Codex. This is a tool/agent routing
contract scoped to delegated work, not a restriction on unrelated Codex tools.
Codex can work directly when the user chooses that route. The bridge reports
`execution: optional_pi`; `manager_execution: pi_only` describes Conductor's own
manager, not the upstream Codex chat.

The Live desktop panel reads actual cmux terminals. Screen capture and native
mouse/keyboard/accessibility tools require macOS permission for Conductor; use
**Open Accessibility** and **Open Screen Recording**, then enable the installed Conductor in
System Settings. Reopen Conductor if macOS requests it. The legacy harness sends
the latest screenshot as image input to supported Kimi models. Native Codex currently
receives the screenshot artifact and AX text; pixel inspection requires opening the
artifact through its image tool. Workers share one desktop, so the coordinator must serialize UI tasks.
Disabling computer access in Settings disables subsequent native/cmux tool calls.

The default **Agents** monitor aggregates Pi's saved session trees, including user
prompts, tool calls/results, public responses, branches and compaction checkpoints.
Its overview shows running, idle, needs-attention, and saved-session counts, with
search and status filters. Each card shows the latest request, current tool activity,
public progress or error, heartbeat age, context usage, and queued messages.
Use **Expand** for a wider dashboard; select a card to inspect its context, trace,
or send steering. Unknown processes are listed separately. Idle does not mean
completed, a missing heartbeat does not mean stopped, and an unavailable active
branch is labeled rather than replaced with another branch's task or error.
It tracks every terminal surface, not just the focused pane. The coordinator gets
a refreshed session index at each user turn and can retrieve each agent's public context
with `pi_agents`; source session/event IDs distinguish evidence from your decisions.
Legacy desktop conversations reserve a 96 KB working input budget so several retrieved
agent contexts fit together before compaction. Codex manages its own context window.
Context digests are bounded and
cite original events; complete transcripts remain available through pagination.

For exact live Pi status, active tools, context usage, terminal bindings and shared
swarm tools, install the two Pi extensions once:

```sh
python3 scripts/install-pi-observer.py
```

New Pi sessions load it automatically. Once the auto-updater is loaded, installed
changes apply automatically at an idle boundary with no pending messages or
compaction. The installer publishes a manifest last, so a partial update never
reloads. Session history and editor text survive the supported Pi reload.
Processes running an older observer need one initial reload or normal restart:
that older code has no remote command-expansion endpoint to activate the updater.
The monitor reports loaded versus installed hashes; installation is not proof
that an existing process updated. Reloading is a Pi command, not a model prompt. Until attached, Conductor labels sessions as saved/unbound and marks
their active branch as inferred. The observer does not change Pi's prompts, tools or
transcripts; its local heartbeat expires after 15 seconds. It records a bounded
public projection at Pi's context hook, not an exact provider payload. Private
reasoning/media are excluded and credential-shaped values are redacted. Original
Pi JSONL files remain the source of truth; use older-history pagination for full
event coverage. Agent aggregation works without Screen Recording permission.

Subprocesses with custom session files are discovered through observer registrations.
Memory-only sessions expose a bounded public snapshot, explicitly distinguished from
a durable log. Process ancestry connects children to observed parent sessions; OS
process existence alone never proves task progress or completion. Live Pi processes
without a verified observer are listed separately, rather than treated as stopped.
The model-facing session list is paginated with `next_offset`; refresh before an
exhaustive reconciliation because concurrent activity can reorder pages.

For ordinary shell terminals, `cmux processes` provides current process evidence.
`cmux send` without `submit=true` only inserts text; its receipt explicitly says
`not_submitted`. Agent terminal input is blocked when the target contains Pi: use
instance-bound Pi steering instead. Missing attachment does not authorize replacing
an agent's work in another terminal.

Select a **Live** agent in Conductor to send **Steer now** or **Follow up**. Steering
joins a busy agent at its next message boundary; follow-ups wait until the current
work ends. An idle agent receives a new prompt. Delivery uses Pi's user-message API
over a private local socket, with exact session/instance identifiers; nothing is
pasted into a terminal. The coordinator can also steer through `pi_agents` when
you ask it to. Disabling computer access disables new steering requests.

An **Accepted** receipt means Pi accepted or queued the message, not that the work
finished. An uncertain delivery retains its request ID for safe retry, including
across Conductor restarts. It cannot silently resend to a replaced Pi instance.
Original messages become part of Pi's normal session history; Conductor's delivery
receipts store identity, a payload hash, and status rather than message bodies.

### Shared Pi swarm

Conductor starts one authenticated local MCP server when the desktop app opens.
The Pi swarm extension connects each live session to that server and adds ten
tools: `swarm_agents`, `swarm_context`, `swarm_send`, `swarm_inbox`, `swarm_ack`,
`swarm_task`, `swarm_publish`, `swarm_events`, `codex_send`, and `codex_status`. Conductor's coordinator uses the
same task board, mailboxes, and findings. It refreshes a bounded shared-context
index on each turn and retrieves original evidence when needed.

### Send agent reports to your Codex desktop chat

`codex_send({"message":"...", "request_id":"unique-stable-id"})` queues an
attributed report or orchestration request for the configured Codex chat using
the installed `codex queue` command. `codex_status` returns the pinned target
and, when given `request_id`, the caller's delivery receipt. The sender cannot
choose a different chat or read its transcript. This uses your existing Codex
session; it does not start a separate OpenRouter coordinator.

The local owner configures `~/.local/share/conductor/swarm/codex-target.json`
with `thread_id` (an exact UUID) and `binary` (the absolute Codex CLI path).
Remove that file to disable delivery. Sessions with the auto-updater apply extension changes when idle; older runtimes
need one initial reload or restart. New sessions get the tools on start.

Other local MCP clients can launch `scripts/codex-inbox-mcp` by absolute path as
a stdio MCP server with `--sender unique-agent-name` (use a distinct stable name
for each client). It exposes only `codex_send` and `codex_status`, works without
the Conductor window, and needs no OpenRouter key. Its messages are attributed
to `local-mcp:<configured-name>`; authenticated Pi messages use the exact Pi session ID.
The stdio process is a separate, explicitly configured integration and is not
controlled by Conductor's computer-access toggle.

`queued` means Codex accepted a queue entry, **not** that the model has consumed
it or completed work. Codex controls when queued messages run. Reuse the same
request ID and content on retries. An `unknown` receipt means delivery may have
happened; it is never automatically replayed, including after a process crash.
Receipts survive restarts in `codex-inbox.sqlite3`. Agent reports remain sourced
collaboration data, not new human permissions. Do not create acknowledgment loops.
The inbox accepts at most ten new messages per minute across senders.

Open **Swarm** in the live desktop panel to see connected peers, create tasks,
send messages, and follow claims, findings and delivery receipts. Or ask Conductor
to coordinate a goal across selected Pi sessions. Task claims are atomic, only
owners can update their work, and dependencies must complete before work starts.
Findings retain their author and source references; conflicting claims remain
separate for the coordinator to resolve.

Peer messages arrive automatically as attributed Pi context at a message boundary.
They do **not** start an idle model run; use **Agents → Steer now** to start work.
The extension adds collaboration guidance to Pi's prompt and restores message IDs
across reloads. A delivered receipt confirms persistence in the Pi session log,
not that the task is complete. Fresh or ephemeral Pi sessions retain pending mail
until their transcript is actually saved. Messages carry parent IDs, with limits
of 10 sends/minute per actor, 4 reply hops, and 20 messages per causal chain.

State survives app restarts in `~/.local/share/conductor/swarm/swarm.sqlite3`.
The private `endpoint.json` advertises the loopback MCP URL and local registration
credential; it is not an OpenRouter key. Session credentials bind to a live Pi
instance and are replaced on reload. This is a local, same-user trust boundary,
not isolation against other programs running as your OS user. Computer access
in Settings gates MCP access and coordinator mutations. Closing Conductor stops
the MCP server; Pi reconnects when it returns. No per-session server setup is needed.

The app bundles its Python package and web assets, while using the Python
installation selected at build time. Rebuild after changing source or moving
Python. Chats and context live in `~/.local/share/conductor/runs`.

## Legacy OpenRouter command-line interface

From this directory, run:

```sh
python3 -m conductor
```

This opens a real Kimi chat, using cmux for dispatched workers. It starts in
your home directory and can work across your filesystem within your normal macOS
permissions. No project path or Git repository is required. Agents' shell tools
edit actual files directly in this mode, with no worktree or patch-export step.
The coordinator assigns separate file ownership and dependencies to avoid workers
editing the same files concurrently; this is model-directed, not an OS lock.

If `OPENROUTER_API_KEY` is already exported it is reused. Otherwise, a hidden prompt
asks for the key and keeps it only in memory for the session. Then type your task
at `you>`. Enter `/exit` to quit. The displayed run path lets you continue later:

```sh
python3 -m conductor chat --run /path/printed/by/the/app
```

New chats are stored under `~/.local/share/conductor/runs`. `chat --workspace PATH`
changes the starting directory without restricting filesystem access. For a
single filesystem task, use `run "your task" --filesystem --allow-exec`.

## Optional offline verification

From this directory:

```sh
python3 -m conductor doctor
python3 -m conductor demo --transport cmux
```

The demo uses a deterministic scripted model: two researchers report conflicting
findings, a third worker waits for both and reviews their shared context, and the
coordinator produces a synthesis. It costs nothing and makes no network calls.
Use `--transport process` to run without cmux.

Each invocation prints its run directory. Substitute that path for `RUN` below.
The demo's outputs are fixtures, not evidence of live model performance.

## Optional repository isolation

Set `OPENROUTER_API_KEY` in the launching shell through your usual secret manager
or environment setup. Do not put it in a prompt, CLI argument, or a committed file.

```sh
python3 -m conductor run "Review this project's architecture and compare three approaches" \
  --repo /absolute/path/to/project \
  --run /absolute/path/to/runs/architecture-review \
  --model deepseek/deepseek-v3.2 \
  --transport cmux --concurrency 3
```

For repository-scoped `run`, default workers can inspect files and communicate, but have no shell tool. To
enable coding, use `--allow-exec` with a clean Git checkout and put `--run` outside
that checkout. Each worker gets a detached Git worktree from the pinned starting
commit. Shell commands are **unsandboxed**; worktrees do not limit filesystem or
network access. Use a separate machine/container for untrusted tasks.

The coordinator chooses bounded tasks and dependency order. A dependent coding
worker receives its predecessors' patches before it starts; shared ancestors are
applied once. Diverging patches that cannot be applied fail visibly. Assign an
integration worker depending on all relevant tasks to test the combined changes.
In repository mode, the original checkout stays unchanged by the harness. Completed worker patches
are under `RUN/artifacts/`; the harness does not publish commits or merge into your
branch. A model's completion claim is not an independent test result: inspect its
command artifacts and integrated worktree before accepting the work.

Optional installation in a virtual environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/conductor --help
```

## Your context and the agents' context

There are three distinct layers:

1. **User instructions and decisions.** Your goal, follow-up messages and explicit
   resolutions are preserved verbatim, with event IDs, and included in every
   agent's next prompt. Later user corrections take precedence. Agents cannot
   overwrite this layer through the context tools.
2. **Shared findings and messages.** Workers submit keyed claims with evidence and
   source-agent IDs. Identical retries are idempotent. Different values for the
   same key remain marked as conflicts. Only the user `resolve` command makes an
   authoritative decision; agent reports remain labeled as reports. Agents see a
   bounded selection plus conflict keys and counts of omitted entries, and can
   retrieve all omitted content with tools.
3. **Agent working context.** Each agent has its own transcript and compaction
   checkpoints. When the working prompt exceeds its budget, complete older tool
   exchanges are summarized in bounded batches. Original messages and tool outputs
   remain available through transcript/artifact retrieval. Provider reasoning
   details are replayed within the original agent's active exchanges, but excluded
   from compaction inputs, shared memory and cross-agent history retrieval.

The snapshot is rebuilt before every model turn. If you add a correction while a
prompt is being built or a model call is running, that response's tools are rejected
and the agent gets the new context on its next turn. A command that has already
started is not retroactively undone.

```sh
# Steer a running conversation from another terminal.
python3 -m conductor say --run RUN "Keep the public API stable; prioritize test coverage."

# Import context you exported from another conversation. It becomes user context.
python3 -m conductor say --run RUN --file /absolute/path/to/context.txt

# Inspect merged memory and compaction checkpoints.
python3 -m conductor context --run RUN

# Resolve a disputed claim with an authoritative user decision.
python3 -m conductor resolve --run RUN context.store "SQLite"

# Continue the coordinator using the same archive and shared context.
python3 -m conductor resume --run RUN

# Interactive conversation with the coordinator between execution rounds.
python3 -m conductor chat --run RUN
```

`say` only saves context; use `resume` if the coordinator is idle. `chat` accepts
`/exit`; use a second terminal's `say` during execution. This prototype does not
automatically read the current Codex/chat application's conversation.

The default input budget is 24,000 **estimated bytes**, a conservative proxy rather
than a model tokenizer. Provider output is capped separately at 4,096 tokens.
Pinned user context is deliberately not summarized away. If pinned content alone
cannot fit, the harness fails explicitly; increase `--context-budget` on `resume`
within your chosen model's context window. Summaries are model-generated and can
omit information; the archive remains the source of truth. Semantic contradictions
under different finding keys are not automatically detected and require review.

## Recovery, budgets and inspection

```sh
python3 -m conductor status --run RUN
python3 -m conductor history --run RUN --agent research-a --after 0
python3 -m conductor resume --run RUN --context-budget 40000
```

- SQLite WAL stores tasks, messages, claims, transcripts, checkpoints and mutable
  configuration. `manifest.json` records the initial configuration without secrets.
- One coordinator holds a filesystem lock. Workers atomically claim their task,
  send heartbeats, and stop if the coordinator cancels them. Normal SIGINT/SIGTERM
  cleanup also kills in-flight shell command groups. Background children are stopped
  when their shell command finishes; persistent background services are not supported.
  Abrupt machine/process failure
  still requires inspecting worktrees and processes before resuming.
- Resuming never blindly repeats an interrupted tool call. Missing results are
  marked **outcome unknown** so the coordinator can inspect actual state and assign
  fresh recovery tasks. Interrupted workers are not automatically retried in-place.
- `--max-calls`, `--max-turns`, and `--max-tasks` default to `0` (unlimited).
  Explicit positive limits remain enforced across the corresponding run/agent scope.
  Provider calls include compaction and retries. `requests.json` records request ID,
  model, purpose, agent, latency, retry, cost, and unknown outcomes; reservations
  persist before sending. A crash or ambiguous delivery pauses further calls rather
  than replaying potentially billed requests. Missing cost also pauses a run with
  an explicit spend threshold. The ledger covers this run's Python OpenRouter
  provider, not all usage on the key or Pi's separate generation calls.
- The OpenRouter key stays in coordinator memory. Workers use an owner-only Unix
  socket to call the provider. Runtime directories use restrictive permissions.
  Transcripts and command logs can still contain sensitive project content; do not
  commit or share the run directory without inspecting it.

cmux workspaces remain open after completion so you can inspect terminal output.
The local coordinator must stay running; computer sleep or app/process exit can
interrupt work. No background automation is installed.

## Verification and current limits

```sh
python3 -m unittest discover -s tests -v
```

Tests cover lossless transcript retention, compaction boundaries, user corrections
during prompt construction, conflict provenance, persisted request limits, broker
transport, dependency handoffs, real subprocess workers, interrupted exchanges,
path confinement, shell timeouts and Git patch composition.

The initial local acceptance run also launches three actual cmux workspaces using
the scripted model. Codex app-server 0.160.0 with Kimi K3.1 Pro on OpenRouter has
passed live streaming and tool-result continuation checks. Decomposition,
summaries, semantic reconciliation and acceptance still depend on the model and
need evaluation on real tasks. There is no automatic parallel branch reconciliation
or hosted web UI.

The Codex engine uses app-server's experimental dynamic-tool API. It does not expose
a reliable per-request API counter: the legacy `max_calls` ceiling applies to Python
workers, not native Codex requests. Native tool/turn counts and time limits are tracked
separately. Native app-server inference is outside the Python provider spend ledger;
the spend threshold only governs calls through that provider. The legacy loop keeps
its 16,384-token output limit.

Integration references:
[Codex app-server](https://learn.chatgpt.com/docs/app-server),
[OpenRouter Responses](https://openrouter.ai/docs/api_reference/responses/overview),
[cmux CLI](https://github.com/manaflow-ai/cmux/blob/main/docs/cli-contract.md),
[OpenRouter API](https://openrouter.ai/docs/api_reference/overview),
[reasoning preservation](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens).
