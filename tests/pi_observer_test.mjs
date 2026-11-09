import assert from 'node:assert/strict';
import { readFileSync, mkdtempSync, rmSync, statSync, existsSync, writeFileSync, mkdirSync } from 'node:fs';
import { createConnection } from 'node:net';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { spawnSync } from 'node:child_process';

const directory = mkdtempSync('/tmp/co-test-');
process.env.CONDUCTOR_PI_OBSERVER_DIR = directory;
const source = readFileSync(new URL('../integrations/pi/conductor-observer.ts', import.meta.url), 'utf8');
const { default: extension } = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
const handlers = new Map();
const deliveries = [];
extension({ on(name, handler) { handlers.set(name, handler); }, sendUserMessage(...args) { if (args[0] === 'Rejected fixture') throw new Error('private internal failure'); deliveries.push(args); } });
let idle = false, currentSession = 'test-session', sessionFile = '/tmp/pi-subagent/task/session.jsonl';
let sessionEntries = [{ type: 'message', id: 'entry-a', message: { role: 'assistant', content: [{type:'thinking',thinking:'private-entry-reasoning'}, {type:'text',text:'Latest subprocess result'}] } }];
const ctx = {
  sessionManager: { getSessionId: () => currentSession, getLeafId: () => 'leaf-a', getSessionFile: () => sessionFile, getEntries: () => sessionEntries, getCwd: () => '/tmp/project', getHeader: () => ({ parentSession: '/tmp/pi-parent/session.jsonl' }) },
  isIdle: () => idle, hasPendingMessages: () => false,
  getContextUsage: () => ({ tokens: 123, contextWindow: 1000, percent: 12.3 }),
  getSystemPrompt: () => 'System prompt. api_key=fixture-secret',
};
const snapshot = () => JSON.parse(readFileSync(join(directory, 'test-session.json'), 'utf8'));
function send(command, socket = snapshot().control_socket) {
  return new Promise((resolve, reject) => {
    const client = createConnection(join(directory, socket));
    let output = '';
    client.on('connect', () => client.write(typeof command === 'string' ? command : JSON.stringify(command) + '\n'));
    client.on('data', chunk => output += chunk);
    client.on('error', reject);
    client.on('end', () => { try { resolve(JSON.parse(output)); } catch (error) { reject(error); } });
  });
}
try {
  await handlers.get('session_start')({}, ctx);
  assert.equal(snapshot().leaf_id, 'leaf-a');
  assert.equal(snapshot().session_file, '/tmp/pi-subagent/task/session.jsonl');
  assert.equal(snapshot().cwd, '/tmp/project');
  assert.equal(snapshot().parent_pid, process.ppid);
  assert(Math.abs(snapshot().process_started_at - (Date.now()/1000-process.uptime())) < 2);
  assert.equal(snapshot().parent_session_file, '/tmp/pi-parent/session.jsonl');
  assert.equal(snapshot().status, 'working');
  assert.equal(snapshot().context_usage.tokens, 123);
  assert.equal(snapshot().recent_entries[0].id, 'entry-a');
  assert.equal(snapshot().recent_entries_truncated, false);
  assert(!JSON.stringify(snapshot().recent_entries).includes('private-entry-reasoning'));
  sessionFile = undefined;
  sessionEntries = Array.from({length: 110}, (_, i) => ({type:'custom',id:String(i),data:'x'.repeat(900)}));
  handlers.get('message_end')({}, ctx);
  assert.equal(snapshot().session_file, null);
  assert.equal(snapshot().recent_entries.at(-1).id, '109');
  assert.equal(snapshot().recent_entries_truncated, true);
  assert(Buffer.byteLength(JSON.stringify(snapshot().recent_entries)) <= 40000);
  sessionEntries = [{type:'message',id:'large-final',parentId:'parent',message:{role:'assistant',content:Array.from({length:8},()=>({type:'text',text:'Latest public output '.repeat(1000)}))}}];
  handlers.get('message_end')({}, ctx);
  assert.equal(snapshot().recent_entries.length, 1);
  assert.equal(snapshot().recent_entries[0].id, 'large-final');
  assert.equal(snapshot().recent_entries[0].parentId, 'parent');
  assert.equal(snapshot().recent_entries[0].message.role, 'assistant');
  assert.equal(snapshot().recent_entries_truncated, true);
  assert.match(snapshot().recent_entries[0].message.content[0].text, /Latest public output/);
  assert.match(snapshot().recent_entries[0].message.content[0].text, /oversized entry truncated/);
  assert(Buffer.byteLength(JSON.stringify(snapshot().recent_entries)) <= 40000);
  handlers.get('tool_execution_start')({ toolCallId: 'call-a', toolName: 'read' }, ctx);
  assert.equal(snapshot().active_tools[0].id, 'call-a');
  const input = { messages: [{ role: 'assistant', content: [
    { type: 'thinking', thinking: 'private-trace-fixture' },
    { type: 'text', text: 'Public outcome' },
    { type: 'toolCall', name: 'read', arguments: { password: 'fixture-password' } },
  ] }] };
  const original = JSON.stringify(input);
  assert.equal(handlers.get('context_with_system')(input, ctx), undefined);
  assert.equal(JSON.stringify(input), original);
  const serialized = JSON.stringify(snapshot());
  for (const secret of ['private-trace-fixture', 'fixture-password', 'fixture-secret']) assert(!serialized.includes(secret));
  assert(serialized.includes('Public outcome'));
  handlers.get('tool_execution_end')({ toolCallId: 'call-a' }, ctx);
  assert.equal(snapshot().active_tools.length, 0);
  const attached = snapshot(), socket = attached.control_socket;
  assert.equal(attached.control_protocol, 'conductor.pi.control');
  assert.equal(attached.control_version, 1);
  assert.equal(statSync(directory).mode & 0o777, 0o700);
  assert.equal(statSync(join(directory, socket)).mode & 0o777, 0o600);
  const command = (request_id, action = 'steer', message = 'Fixture prompt') => ({ request_id,
    action, message, session_id: attached.session_id, instance_id: attached.instance_id });
  const ping = await send(command('ping', 'ping'));
  assert.equal(ping.status, 'accepted'); assert.equal(ping.delivery, 'status'); assert.equal(deliveries.length, 0);
  assert.equal((await send({ ...command('wrong'), session_id: 'other' })).status, 'rejected');
  assert.equal((await send({ ...command('wrong'), instance_id: 'stale' })).status, 'rejected');
  assert.equal((await send(command('wrong', 'shell'))).status, 'rejected');
  assert.equal((await send(command('empty', 'steer', ' '))).status, 'rejected');
  assert.equal(deliveries.length, 0);
  const steer = await send(command('steer'));
  assert.equal(steer.delivery, 'steering'); assert.deepEqual(deliveries[0], ['Fixture prompt', { deliverAs: 'steer' }]);
  assert.deepEqual(await send(command('steer')), steer); assert.equal(deliveries.length, 1);
  assert.equal((await send(command('steer', 'steer', 'Different'))).status, 'rejected');
  assert.equal(deliveries.length, 1);
  assert.equal((await send(command('follow', 'follow_up'))).delivery, 'follow_up');
  assert.deepEqual(deliveries[1], ['Fixture prompt', { deliverAs: 'followUp' }]);
  idle = true;
  assert.equal((await send(command('idle', 'follow_up'))).delivery, 'prompt');
  assert.deepEqual(deliveries[2], ['Fixture prompt']);
  assert.equal((await send(command('large', 'steer', 'x'.repeat(40960)))).status, 'rejected');
  assert.equal((await send('invalid-json\n')).status, 'rejected');
  assert.equal(deliveries.length, 3);
  const rejected = await send(command('rejected', 'steer', 'Rejected fixture'));
  assert.equal(rejected.status, 'rejected');
  assert(!JSON.stringify(rejected).includes('private internal failure'));
  assert.deepEqual(await send(command('rejected', 'steer', 'Rejected fixture')), rejected);
  currentSession = 'changed-session';
  assert.equal((await send(command('changed'))).status, 'rejected');
  currentSession = 'test-session';
  await handlers.get('session_shutdown')({}, ctx);
  assert.equal(snapshot().status, 'closed');
  assert.equal(snapshot().control_socket, null);
  assert(!existsSync(join(directory, socket)));
  await assert.rejects(send(command('closed'), socket));
  await handlers.get('session_start')({ reason: 'reload' }, ctx);
  assert.notEqual(snapshot().instance_id, attached.instance_id);
  assert.equal((await send(command('stale-after-reload'))).status, 'rejected');
  assert.equal(deliveries.length, 3);
  await handlers.get('session_shutdown')({}, ctx);
  const child = spawnSync(process.execPath, ['--input-type=module', '-e', `
    const {default: extension} = await import(${JSON.stringify('data:text/javascript;base64,' + Buffer.from(source).toString('base64'))});
    const handlers = new Map();
    extension({on: (name, handler) => handlers.set(name, handler)});
    const ctx = {sessionManager: {getSessionId: () => 'subprocess-session', getLeafId: () => null,
      getSessionFile: () => '/tmp/custom-subprocess/session.jsonl', getCwd: () => '/tmp/worker'},
      isIdle: () => true, hasPendingMessages: () => false, getContextUsage: () => null, getSystemPrompt: () => ''};
    await handlers.get('session_start')({}, ctx);
    // No session_shutdown: reproduce an SDK embedding ending naturally.
  `], {encoding: 'utf8', timeout: 5000});
  assert.equal(child.status, 0, child.stderr);
  const childSnapshot = JSON.parse(readFileSync(join(directory, 'subprocess-session.json'), 'utf8'));
  assert.equal(childSnapshot.status, 'closed');
  assert.equal(childSnapshot.last_event, 'process_exit');
  assert.equal(childSnapshot.control_socket, null);
  assert.equal(childSnapshot.parent_pid, process.pid);
  assert.equal(childSnapshot.session_file, '/tmp/custom-subprocess/session.jsonl');
  assert.equal(childSnapshot.parent_session_file, null);
  console.log('Pi observer lifecycle, public context, addressed steering, idempotency and socket isolation passed.');
} finally {
  await handlers.get('session_shutdown')({}, ctx);
  rmSync(directory, { recursive: true, force: true });
}

// Exercise installed-file detection without loading or contacting a real Pi session.
const runtimeDirectory = mkdtempSync('/tmp/co-runtime-');
const runtimeHandlers = new Map(), commands = new Map(), reloadMessages = [];
let runtimeIdle = false, pending = false, reloadCount = 0;
const names = ['conductor-observer.ts', 'conductor-swarm.ts'];
function installFixture(swarm) {
  writeFileSync(join(runtimeDirectory, names[0]), source);
  writeFileSync(join(runtimeDirectory, names[1]), swarm);
  writeFileSync(join(runtimeDirectory, 'conductor-runtime.json'), JSON.stringify({hashes: Object.fromEntries(names.map(name => [name, createHash('sha256').update(readFileSync(join(runtimeDirectory, name))).digest('hex')]))}));
}
installFixture('// initial');
writeFileSync(join(runtimeDirectory, 'observer.mjs'), source);
process.env.CONDUCTOR_PI_OBSERVER_DIR = join(runtimeDirectory, 'snapshots');
const runtimeContext = {...ctx, isIdle: () => runtimeIdle, hasPendingMessages: () => pending,
  reload: async () => { reloadCount++; }};
const runtimeSnapshot = () => JSON.parse(readFileSync(join(runtimeDirectory, 'snapshots/test-session.json')));
try {
  const {default: runtimeExtension} = await import(pathToFileURL(join(runtimeDirectory, 'observer.mjs')).href);
  runtimeExtension({on: (name, fn) => runtimeHandlers.set(name, fn), registerCommand: (name, command) => commands.set(name, command), sendUserMessage: (...args) => reloadMessages.push(args)});
  await runtimeHandlers.get('session_start')({}, runtimeContext);
  assert.equal(runtimeSnapshot().extension_runtime.update_state, 'current');
  installFixture('// updated');
  runtimeHandlers.get('agent_settled')({}, runtimeContext);
  assert.equal(reloadMessages.length, 0); // working/compaction: isIdle is false
  assert.equal(runtimeSnapshot().extension_runtime.update_state, 'waiting_for_idle');
  runtimeIdle = true; pending = true;
  runtimeHandlers.get('agent_settled')({}, runtimeContext);
  assert.equal(reloadMessages.length, 0);
  pending = false;
  runtimeHandlers.get('agent_settled')({}, runtimeContext);
  assert.deepEqual(reloadMessages, [['/conductor-reload-runtime', {expandPromptTemplates: true}]]);
  assert.notDeepEqual(runtimeSnapshot().extension_runtime.loaded_hashes, runtimeSnapshot().extension_runtime.installed_hashes);
  runtimeHandlers.get('agent_settled')({}, runtimeContext);
  assert.equal(reloadMessages.length, 1);
  // Recheck at the command boundary in case work started after dispatch.
  pending = true;
  await commands.get('conductor-reload-runtime').handler('', runtimeContext);
  assert.equal(reloadCount, 0);
  pending = false;
  runtimeHandlers.get('agent_settled')({}, runtimeContext);
  await commands.get('conductor-reload-runtime').handler('', runtimeContext);
  assert.equal(reloadCount, 1);
  await runtimeHandlers.get('session_shutdown')({}, runtimeContext);
  await commands.get('conductor-reload-runtime').handler('', runtimeContext);
  assert.equal(reloadCount, 1);
  // A newly evaluated runtime takes the new baseline and must not loop.
  const {default: freshExtension} = await import(pathToFileURL(join(runtimeDirectory, 'observer.mjs')).href + '?new-runtime');
  freshExtension({on: (name, fn) => runtimeHandlers.set(name, fn), registerCommand: (name, command) => commands.set(name, command), sendUserMessage: (...args) => reloadMessages.push(args)});
  await runtimeHandlers.get('session_start')({}, runtimeContext);
  runtimeHandlers.get('agent_settled')({}, runtimeContext);
  assert.equal(reloadMessages.length, 2);
  assert.equal(runtimeSnapshot().extension_runtime.update_state, 'current');
  writeFileSync(join(runtimeDirectory, names[1]), '// incomplete installation');
  runtimeHandlers.get('agent_settled')({}, runtimeContext);
  assert.equal(runtimeSnapshot().extension_runtime.update_state, 'installing');
  assert.equal(reloadMessages.length, 2);
  console.log('Pi idle auto-update, pending-work guard, install transaction and runtime lifecycle passed.');
} finally {
  await runtimeHandlers.get('session_shutdown')?.({}, runtimeContext);
  rmSync(runtimeDirectory, {recursive: true, force: true});
}

// Native compaction never becomes a user message or runs across active work.
const compactDirectory = mkdtempSync('/tmp/co-compact-');
process.env.CONDUCTOR_PI_OBSERVER_DIR = compactDirectory;
const compactHandlers = new Map(), nativeCalls = [], compactMessages = [];
let compactIdle = false, compactPending = false, compactLeaf = 'before', nativeNow = Date.now;
const compactContext = {...ctx,
  sessionManager: {...ctx.sessionManager, getLeafId: () => compactLeaf},
  isIdle: () => compactIdle, hasPendingMessages: () => compactPending,
  compact: options => nativeCalls.push(options),
};
const compactSnapshot = () => JSON.parse(readFileSync(join(compactDirectory, 'test-session.json')));
const settle = async () => { compactHandlers.get('agent_settled')({}, compactContext); await new Promise(resolve => setImmediate(resolve)); };
function compactSend(request_id, action = 'compact') {
  const state = compactSnapshot();
  return new Promise((resolve, reject) => {
    const client = createConnection(join(compactDirectory, state.control_socket));
    let output = '';
    client.on('connect', () => client.write(JSON.stringify({session_id: state.session_id, instance_id: state.instance_id, request_id, action, ...(action === 'steer' ? {message:'Urgent clarification'} : {})}) + '\n'));
    client.on('data', data => output += data);
    client.on('error', reject);
    client.on('end', () => resolve(JSON.parse(output)));
  });
}
try {
  extension({on: (name, handler) => compactHandlers.set(name, handler), sendUserMessage: (...args) => compactMessages.push(args)});
  await compactHandlers.get('session_start')({}, compactContext);
  assert.equal((await compactSend('compact-one')).compaction.state, 'queued');
  assert.equal((await compactSend('compact-one')).status, 'accepted');
  assert.equal(nativeCalls.length, 0);
  assert.equal((await compactSend('urgent', 'steer')).status, 'accepted');
  assert.equal(compactMessages.length, 1);
  compactIdle = true; compactPending = true;
  await settle(); assert.equal(nativeCalls.length, 0);
  compactPending = false;
  compactHandlers.get('tool_execution_start')({toolCallId:'active',toolName:'bash'}, compactContext);
  await settle(); assert.equal(nativeCalls.length, 0);
  compactHandlers.get('tool_execution_end')({toolCallId:'active'}, compactContext);
  await settle(); assert.equal(nativeCalls.length, 1);
  assert.equal(compactSnapshot().compaction.state, 'running');
  assert.equal((await compactSend('during', 'steer')).status, 'rejected');
  assert.match(nativeCalls[0].customInstructions, /ownership/);
  assert.match(nativeCalls[0].customInstructions, /evidence/);
  compactLeaf = 'after'; nativeCalls[0].onComplete({summary:'private summary is not published'});
  assert.equal((await compactSend('compact-one')).compaction.state, 'completed');
  assert(!JSON.stringify(compactSnapshot()).includes('private summary'));
  assert.equal((await compactSend('repeat-leaf')).status, 'rejected');
  compactLeaf = 'next';
  assert.equal((await compactSend('cooldown')).status, 'rejected');
  Date.now = () => nativeNow() + 301000;
  assert.equal((await compactSend('compact-two')).status, 'accepted');
  await new Promise(resolve => setImmediate(resolve));
  nativeCalls[1].onError(new Error('private native exception'));
  assert.equal(compactSnapshot().compaction.state, 'failed');
  assert(!JSON.stringify(compactSnapshot()).includes('private native exception'));
  assert.equal(compactMessages.length, 1); // Only the explicit urgent steering message.
  compactLeaf = 'queued-before-shutdown'; compactIdle = false;
  Date.now = () => nativeNow() + 602000;
  assert.equal((await compactSend('compact-three')).compaction.state, 'queued');
  await compactHandlers.get('session_shutdown')({}, compactContext);
  assert.equal(compactSnapshot().compaction.state, 'failed');
  assert.equal(nativeCalls.length, 2);
  await compactHandlers.get('session_start')({}, compactContext);
  assert.equal((await compactSend('same-leaf-after-reload')).status, 'rejected');
  nativeCalls[0].onComplete({}); // Old callbacks cannot mutate a new attachment.
  assert.equal(compactSnapshot().compaction.state, 'failed');
  const journal = join(compactDirectory, 'test-session.compaction.json');
  const journalTemporary = `${journal}.${process.pid}.tmp`;
  compactLeaf = 'persistence-start'; Date.now = () => nativeNow() + 903000;
  assert.equal((await compactSend('compact-four')).compaction.state, 'queued');
  mkdirSync(journalTemporary); // Deterministic write failure without changing the durable intent.
  compactIdle = true;
  await settle();
  assert.equal(nativeCalls.length, 2);
  assert.equal(compactSnapshot().compaction.persistence_error, true);
  assert.equal(JSON.parse(readFileSync(journal)).state, 'queued');
  assert.equal((await compactSend('retry-persistence')).status, 'rejected');
  rmSync(journalTemporary, {recursive:true});
  await compactHandlers.get('session_shutdown')({}, compactContext);
  await compactHandlers.get('session_start')({}, compactContext);
  assert.equal(compactSnapshot().compaction.state, 'failed'); // Interrupted intent never replays.
  compactLeaf = 'persistence-finish'; Date.now = () => nativeNow() + 1204000;
  assert.equal((await compactSend('compact-five')).status, 'accepted');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(nativeCalls.length, 3);
  mkdirSync(journalTemporary);
  assert.doesNotThrow(() => nativeCalls[2].onComplete({}));
  assert.equal(compactSnapshot().compaction.persistence_error, true);
  assert.equal(JSON.parse(readFileSync(journal)).state, 'running');
  rmSync(journalTemporary, {recursive:true});
  await compactHandlers.get('session_shutdown')({}, compactContext);
  await compactHandlers.get('session_start')({}, compactContext);
  compactLeaf = 'persistence-intent'; Date.now = () => nativeNow() + 1505000;
  mkdirSync(journalTemporary);
  const unpersisted = await compactSend('compact-six');
  assert.equal(unpersisted.status, 'rejected');
  assert.match(unpersisted.error, /no compaction started/);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(nativeCalls.length, 3);
  rmSync(journalTemporary, {recursive:true});
  console.log('Pi native compaction deferral, evidence preservation, callbacks, cooldown and restart guards passed.');
} finally {
  Date.now = nativeNow;
  await compactHandlers.get('session_shutdown')?.({}, compactContext);
  rmSync(compactDirectory, {recursive:true, force:true});
}
