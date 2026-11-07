# Conductor execution ownership

Conductor runs its own model to manage Pi agents and exchanges context and
orchestration with the user's pinned Codex chat. Codex owns upstream goals and
review; Conductor owns delegated coordination. Preserve both directions of the
bridge and avoid dispatching the same work independently from both managers.

Only data generation is required to route through Conductor. Codex may execute
platform operations (especially v2), code changes, inspection, configuration,
and verification directly. Data writes are not synonymous with data generation.
Use `conductor_bypass` for an explicit request-scoped direct route; it does not
change permissions or other chats. Do not duplicate already delegated work.

For work explicitly delegated to Conductor, route operational data writes and environment-related work
(including environment inspection, configuration, builds, runs, deployment, and
verification) to execute in Pi agents. Do not perform those operations directly
with Conductor's own execution tools. Never silently duplicate delegated work locally
when Pi is disconnected. This routing contract does not restrict direct Codex work
outside that delegation. Follow the user's current choice of execution route and
preserve all existing approval and scope requirements.

Use the `conductor-pi` MCP: `pi_sessions`, `pi_context`, `pi_dispatch`, `pi_result`,
and `pi_review`, or delegate management with `conductor_send` and inspect
`conductor_result`. Select the relevant exact session and live instance from context;
do not guess based on recency. Give explicit ownership and acceptance criteria.
Pi completion is a report; inspect evidence before accepting it. Environment
verification for that delegated work stays with its Pi owner. Independent ready tasks may go to separate
Pi sessions. Conductor itself has no data/environment execution tool.

For requests received through Conductor, publish progress and the final response
with `conductor_reply`, using the supplied chat/request IDs. If the current Codex
turn has not loaded newly registered MCP tools, use `scripts/conductor-pi-mcp`
with `--call TOOL` and JSON arguments on stdin; it invokes the same implementation.

Local edits/builds/tests needed to maintain this Conductor application and its
adapter tests are within the current implementation task. They do not authorize
operations on the user's application data, environments, or running Pi workloads.
Do not manipulate those workloads to test the bridge; use isolated fixtures.
