// Conductor Pi extension: public observation and explicitly addressed local user-message control.
import { mkdirSync, writeFileSync, renameSync, chmodSync, lstatSync, unlinkSync, readFileSync } from 'node:fs';
import { createServer } from 'node:net';
import { randomUUID, createHash } from 'node:crypto';
import { homedir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const extensionDirectory = import.meta.url.startsWith('file:') ? dirname(fileURLToPath(import.meta.url)) : undefined;
const extensionNames = ['conductor-observer.ts', 'conductor-swarm.ts'];
function sourceHashes() {
  try {
    if (!extensionDirectory) return null;
    return Object.fromEntries(extensionNames.map(name => [name, createHash('sha256').update(readFileSync(join(extensionDirectory, name))).digest('hex')]));
  } catch { return null; }
}
// Capture once when Pi evaluates this runtime, not when its next timer fires.
const loadedSourceHashes = sourceHashes();
const processStartedAt = Date.now() / 1000 - process.uptime();

export function publicValue(value, depth = 0) {
  if (depth > 16) return '[truncated]';
  if (typeof value === 'string') return value
    .replace(/\b(?:sk-(?:or-v1-)?|gh[pousr]_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]{8,}/g, '[redacted]')
    .replace(/\b(?:AKIA|ASIA)[A-Z0-9]{16}\b/g, '[redacted]')
    .replace(/\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/g, '[redacted]')
    .replace(/\bBearer\s+[A-Za-z0-9._~+/=-]+/gi, '[redacted]')
    .replace(/(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|authorization)\s*[=:]\s*["']?[^\s"'&,;]+/gi, '[redacted]')
    .replace(/-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-]*PRIVATE KEY-----|$)/g, '[redacted]')
    .slice(0, 16000);
  if (Array.isArray(value)) return value.slice(0, 200).map(v => publicValue(v, depth + 1)).filter(v => v !== undefined);
  if (value && typeof value === 'object') {
    if (/thinking|reasoning|image|audio/i.test(String(value.type || ''))) return undefined;
    return Object.fromEntries(Object.entries(value).filter(([k]) => !/thinking|reasoning|signature/i.test(k)).slice(0, 100).map(([k, v]) =>
      [k, /^(?:(?:.*[_-])?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|password|passwd|secret|credential)s?|token)$/i.test(k) ? '[redacted]' : publicValue(v, depth + 1)]));
  }
  return value;
}

export default function conductorObserver(pi) {
  const directory = process.env.CONDUCTOR_PI_OBSERVER_DIR || join(homedir(), '.local/share/conductor/pi');
  let timer, context, requestContext, lastEvent = 'session_start';
  const tools = new Map();
  let control, instanceId, socketName, sessionId, closed = true;
  const clients = new Set(), requests = new Map();
  const maxBytes = 40960;
  let compaction = null, compactTimer;
  const compactInstructions = 'Preserve the current task and user constraints, exact paths and identifiers, verified evidence and unresolved checks, ownership boundaries, and pending handoffs/dependencies. Distinguish reported results from accepted results. Retain actionable next steps. Use native session context; do not expose private reasoning.';
  const compactBusy = () => ['queued', 'running'].includes(compaction?.state);
  function saveCompaction() {
    const file = join(directory, `${sessionId}.compaction.json`), temporary = `${file}.${process.pid}.tmp`;
    try {
      writeFileSync(temporary, JSON.stringify(compaction), {mode: 0o600});
      renameSync(temporary, file);
      return true;
    } catch {
      // Keep the last durable intent intact. Never let observer storage failure
      // escape into Pi or authorize retry of an uncertain native operation.
      compaction = {...compaction, state: 'failed', persistence_error: true,
        error: 'Compaction state could not be persisted; inspect the original session before retrying'};
      try { unlinkSync(temporary); } catch {}
      return false;
    }
  }
  function finishCompaction(state) {
    compaction = {...compaction, state, finished_at: Date.now()/1000,
      ...(state === 'completed' ? {result_leaf_id: context.sessionManager.getLeafId()} : {error: 'Native compaction failed or attachment stopped; inspect session before retrying'})};
    saveCompaction();
    if (context) publish(context);
  }
  function advanceCompaction() {
    clearImmediate(compactTimer);
    // Never invoke native compact (which aborts a run) inside settled handlers.
    compactTimer = setImmediate(() => {
      if (closed || compaction?.state !== 'queued' || context?.sessionManager.getSessionId() !== sessionId || !context?.isIdle() || context.hasPendingMessages() || tools.size || reloadDispatched) return;
      compaction = {...compaction, state: 'running', started_at: Date.now()/1000, leaf_id: context.sessionManager.getLeafId()};
      if (!saveCompaction()) { publish(context); return; }
      publish(context);
      const expected = instanceId;
      try {
        context.compact({customInstructions: compactInstructions,
          onComplete: () => { if (!closed && instanceId === expected && context.sessionManager.getSessionId() === sessionId) finishCompaction('completed'); },
          onError: () => { if (!closed && instanceId === expected && context.sessionManager.getSessionId() === sessionId) finishCompaction('failed'); }});
      } catch { finishCompaction('failed'); }
    });
  }
  let installedSourceHashes = loadedSourceHashes, updateState = 'current', reloadDispatched = false;
  const reloadCommand = 'conductor-reload-runtime';
  function inspectUpdate() {
    installedSourceHashes = sourceHashes();
    if (!loadedSourceHashes || !installedSourceHashes) return updateState = 'unavailable';
    if (JSON.stringify(installedSourceHashes) === JSON.stringify(loadedSourceHashes)) return updateState = 'current';
    // The installer publishes its manifest last; never load a half-installed pair.
    try {
      const manifest = JSON.parse(readFileSync(join(extensionDirectory, 'conductor-runtime.json'), 'utf8'));
      if (!extensionNames.every(name => manifest.hashes?.[name] === installedSourceHashes[name])) return updateState = 'installing';
    } catch { return updateState = 'installing'; }
    if (!pi.registerCommand) return updateState = 'reload_required';
    if (!reloadDispatched) updateState = 'waiting_for_idle';
    return updateState;
  }
  function checkUpdate(ctx) {
    inspectUpdate();
    if (compactBusy() || tools.size || closed || reloadDispatched || updateState !== 'waiting_for_idle' || !ctx?.isIdle() || ctx.hasPendingMessages()) return;
    reloadDispatched = true; updateState = 'reload_requested';
    try {
      // Registered command dispatch never becomes a model prompt. Pi defers it until
      // all agent_settled handlers finish, preserving pending work and editor text.
      pi.sendUserMessage(`/${reloadCommand}`, { expandPromptTemplates: true });
    } catch { updateState = 'reload_failed'; }
  }
  pi.registerCommand?.(reloadCommand, {
    description: 'Apply installed Conductor updates when this session is idle',
    handler: async (_args, ctx) => {
      if (compactBusy() || tools.size || closed || !ctx.isIdle() || ctx.hasPendingMessages()) {
        reloadDispatched = false; updateState = 'waiting_for_idle'; return;
      }
      if (inspectUpdate() === 'current' || updateState === 'installing' || updateState === 'unavailable') return;
      try { await ctx.reload(); return; }
      catch { updateState = 'reload_failed'; }
    },
  });
  function reply(command, status, delivery, error) {
    return { request_id: typeof command?.request_id === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(command.request_id) ? command.request_id : null,
      session_id: sessionId, instance_id: instanceId, status, delivery, ...(error ? { error } : {}) };
  }
  function commandReply(command, expectedInstance) {
    const reject = error => reply(command, 'rejected', 'none', error);
    if (!command || typeof command !== 'object' || Array.isArray(command)) return reject('Expected a JSON command object');
    if (closed || expectedInstance !== instanceId || !context || context.sessionManager.getSessionId() !== sessionId)
      return reject('Session attachment is closed or changed');
    if (command.session_id !== sessionId || command.instance_id !== instanceId) return reject('Session or instance does not match');
    if (typeof command.request_id !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(command.request_id)) return reject('Invalid request_id');
    if (!['steer', 'follow_up', 'ping', 'compact'].includes(command.action)) return reject('Unsupported action');
    if (['steer', 'follow_up'].includes(command.action) && (typeof command.message !== 'string' || !command.message.trim())) return reject('A nonempty message is required');
    const hash = createHash('sha256').update(JSON.stringify([command.session_id, command.instance_id, command.action, command.message ?? null])).digest('hex');
    const prior = requests.get(command.request_id);
    if (prior) return prior.hash === hash ? {...prior.result, ...(command.action === 'compact' && compaction?.request_id === command.request_id ? {compaction} : {})} : reject('request_id was already used with a different payload');
    if (requests.size >= 4096) return reject('Attachment request capacity reached; reload the attachment');
    let result;
    if (command.action === 'compact') {
      if (typeof context.compact !== 'function') return reject('Native compaction API unavailable');
      if (compaction?.persistence_error) return reject('Compaction persistence failed; inspect the original session before retrying');
      if (compactBusy() || reloadDispatched) return reject('Compaction or runtime reload already pending');
      const leaf = context.sessionManager.getLeafId();
      if (compaction && (leaf === compaction.leaf_id || leaf === compaction.result_leaf_id || Date.now()/1000 - compaction.requested_at < 300)) return reject('Compaction already attempted on this leaf or within the five-minute cooldown');
      compaction = {request_id: command.request_id, instance_id: instanceId, state: 'queued', leaf_id: leaf, requested_at: Date.now()/1000};
      if (!saveCompaction()) { publish(context); return reject('Compaction intent could not be persisted; no compaction started'); }
      publish(context); advanceCompaction();
      result = {...reply(command, 'accepted', 'queued'), compaction};
    } else if (command.action === 'ping') {
      result = { ...reply(command, 'accepted', 'status'), idle: context.isIdle(), pending_messages: context.hasPendingMessages(), compaction };
    } else {
      if (compaction?.state === 'running') return reject('Native compaction is running; retry after its terminal result');
      const idle = context.isIdle();
      const delivery = idle ? 'prompt' : command.action === 'steer' ? 'steering' : 'follow_up';
      try {
        // Pi's void API acknowledges submission/queuing, never completion. No command expansion.
        if (idle) pi.sendUserMessage(command.message);
        else pi.sendUserMessage(command.message, { deliverAs: command.action === 'steer' ? 'steer' : 'followUp' });
        result = reply(command, 'accepted', delivery);
      } catch { result = reject('Pi rejected the message submission'); }
    }
    requests.set(command.request_id, { hash, result });
    return result;
  }
  async function stopControl() {
    closed = true;
    if (compactBusy()) finishCompaction('failed');
    clearImmediate(compactTimer);
    for (const client of clients) client.destroy();
    clients.clear();
    const server = control, oldSocket = socketName;
    control = undefined; socketName = undefined;
    if (server?.listening) await new Promise(resolve => server.close(resolve));
    if (oldSocket) { try { unlinkSync(join(directory, oldSocket)); } catch {} }
  }
  async function startControl(ctx) {
    await stopControl();
    mkdirSync(directory, { recursive: true, mode: 0o700 });
    const info = lstatSync(directory);
    if (!info.isDirectory() || info.isSymbolicLink() || (process.getuid && info.uid !== process.getuid())) throw new Error('Unsafe observer directory');
    chmodSync(directory, 0o700);
    instanceId = randomUUID(); sessionId = ctx.sessionManager.getSessionId(); requests.clear();
    try {
      compaction = JSON.parse(readFileSync(join(directory, `${sessionId}.compaction.json`), 'utf8'));
      if (compactBusy()) finishCompaction('failed');
    } catch { compaction = null; }
    const expectedInstance = instanceId;
    socketName = `c-${process.pid}-${instanceId.slice(0, 12)}.sock`;
    const server = createServer(client => {
      clients.add(client); client.setTimeout(5000, () => client.destroy());
      client.on('close', () => clients.delete(client)); client.on('error', () => {});
      let buffer = Buffer.alloc(0), finished = false;
      client.on('data', chunk => {
        if (finished) return;
        buffer = Buffer.concat([buffer, chunk]);
        if (buffer.length > maxBytes) {
          finished = true; client.end(JSON.stringify(reply(null, 'rejected', 'none', 'Command exceeds 40960 bytes')) + '\n'); return;
        }
        const newline = buffer.indexOf(10);
        if (newline < 0) return;
        finished = true;
        let result;
        try { result = commandReply(JSON.parse(buffer.subarray(0, newline).toString('utf8')), expectedInstance); }
        catch { result = reply(null, 'rejected', 'none', 'Invalid JSON command'); }
        client.end(JSON.stringify(result) + '\n');
      });
    });
    control = server;
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(join(directory, socketName), () => { server.removeListener('error', reject); resolve(); }); });
    chmodSync(join(directory, socketName), 0o600); server.unref();
    server.on('error', () => { closed = true; });
    closed = false;
  }
  function onProcessExit() {
    // SDK/embedded subprocesses may exit without emitting Pi's session_shutdown.
    // Only synchronous work is permitted here; do not intercept signals or keep Pi alive.
    closed = true;
    if (socketName) { try { unlinkSync(join(directory, socketName)); } catch {} }
    lastEvent = 'process_exit';
    if (context) publish(context, 'closed');
  }
  function publish(ctx, status) {
    try {
      const sessionId = ctx.sessionManager.getSessionId();
      if (!/^[A-Za-z0-9_-]{1,128}$/.test(sessionId)) return;
      const entries = ctx.sessionManager.getEntries?.() || [], recentEntries = [];
      let remaining = 39998;
      for (let i = entries.length - 1; i >= 0 && recentEntries.length < 100; i--) {
        let entry = publicValue(entries[i]);
        if (!entry) continue;
        let size = Buffer.byteLength(JSON.stringify(entry), 'utf8') + 1;
        if (size > remaining) {
          if (recentEntries.length) break;
          // A large final tool/message must not erase all memory-only session context.
          // Keep native entry identity and role, with a bounded public textual projection.
          const original = entry;
          const content = original.message?.content ?? original.content ?? original.data ?? original;
          let text = typeof content === 'string' ? content : JSON.stringify(content);
          const marker = '\n[Conductor: oversized entry truncated]';
          entry = { id: original.id, parentId: original.parentId, type: original.type,
            timestamp: original.timestamp, truncated: true,
            ...(original.message ? { message: { role: original.message.role, content: [{ type: 'text', text: '' }] } } : { content: '' }) };
          do {
            text = text.slice(0, Math.min(8000, Math.floor(text.length / 2)));
            if (entry.message) entry.message.content[0].text = text + marker;
            else entry.content = text + marker;
            size = Buffer.byteLength(JSON.stringify(entry), 'utf8') + 1;
          } while (size > remaining && text.length);
          if (size > remaining) break;
        }
        remaining -= size; recentEntries.unshift(entry);
      }
      const snapshot = {
        version: 1, control_protocol: 'conductor.pi.control', control_version: 1, instance_id: instanceId,
        control_socket: !closed ? socketName : null, session_id: sessionId, pid: process.pid, observed_at: Date.now() / 1000,
        compaction,
        extension_runtime: { loaded_hashes: loadedSourceHashes, installed_hashes: installedSourceHashes,
          update_state: updateState, hash_source: 'extension files at runtime initialization' },
        leaf_id: ctx.sessionManager.getLeafId(),
        session_file: publicValue(ctx.sessionManager.getSessionFile?.() ?? null),
        cwd: publicValue(ctx.sessionManager.getCwd?.() ?? ctx.cwd ?? null),
        parent_pid: process.ppid, process_started_at: processStartedAt,
        recent_entries: recentEntries, recent_entries_truncated: recentEntries.length < entries.length || recentEntries.some(entry => entry.truncated === true),
        parent_session_file: publicValue(ctx.sessionManager.getHeader?.()?.parentSession ?? null),
        status: status || (compaction?.state === 'running' ? 'working' : ctx.isIdle() ? 'idle' : 'working'),
        pending_messages: ctx.hasPendingMessages(), last_event: lastEvent,
        context_usage: publicValue(ctx.getContextUsage()),
        active_tools: [...tools.values()],
        system_prompt: publicValue(ctx.getSystemPrompt()),
        request_context: requestContext,
      };
      mkdirSync(directory, { recursive: true, mode: 0o700 });
      const file = join(directory, `${sessionId}.json`), temporary = `${file}.${process.pid}.tmp`;
      writeFileSync(temporary, JSON.stringify(snapshot), { mode: 0o600 });
      renameSync(temporary, file);
    } catch { /* Observation must never break the user's agent. */ }
  }
  pi.on('session_start', async (_event, ctx) => {
    process.removeListener('exit', onProcessExit);
    process.once('exit', onProcessExit);
    clearInterval(timer); context = ctx; requestContext = undefined; tools.clear();
    lastEvent = 'session_start';
    try { await startControl(ctx); } catch { await stopControl(); }
    checkUpdate(ctx); publish(ctx);
    timer = setInterval(() => { checkUpdate(context); if (context) publish(context); }, 3000);
    timer.unref();
  });
  pi.on('session_shutdown', async (_event, ctx) => {
    process.removeListener('exit', onProcessExit);
    clearInterval(timer); await stopControl(); lastEvent = 'session_shutdown'; publish(ctx, 'closed'); context = undefined;
  });
  for (const name of ['agent_start', 'agent_settled', 'turn_end', 'message_end', 'session_tree', 'session_compact', 'model_select']) {
    pi.on(name, (_event, ctx) => { context = ctx; lastEvent = name; if (name === 'agent_settled') { advanceCompaction(); checkUpdate(ctx); } publish(ctx); });
  }
  pi.on('tool_execution_start', (event, ctx) => {
    tools.set(event.toolCallId, { id: event.toolCallId, name: event.toolName, started_at: Date.now() / 1000 });
    lastEvent = 'tool_execution_start'; publish(ctx);
  });
  pi.on('tool_execution_end', (event, ctx) => {
    tools.delete(event.toolCallId); lastEvent = 'tool_execution_end'; publish(ctx);
  });
  pi.on('context_with_system', (event, ctx) => {
    // Observe a bounded public projection, never provider credentials or private reasoning.
    const messages = [], all = event.messages || [];
    let remaining = 80000;
    for (let i = all.length - 1; i >= 0 && messages.length < 100; i--) {
      const message = publicValue(all[i]);
      if (!message) continue;
      const size = JSON.stringify(message).length;
      if (size > remaining) break;
      remaining -= size; messages.unshift(message);
    }
    requestContext = { observed_at: Date.now() / 1000, message_count: all.length, messages,
      truncated: messages.length < all.length,
      source: 'Pi context_with_system hook; bounded public projection. Later extensions/provider serialization may change the request.' };
    lastEvent = 'context_with_system'; publish(ctx);
    // No return value: do not modify context.
  });
}
