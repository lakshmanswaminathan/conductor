import assert from 'node:assert/strict';
import { readFileSync, writeFileSync, appendFileSync, unlinkSync, renameSync, mkdtempSync, rmSync } from 'node:fs';
import { createServer } from 'node:http';
import { join } from 'node:path';
import { homedir } from 'node:os';
import { createRequire } from 'node:module';
const require = createRequire(process.env.PI_PACKAGE_JSON || join(homedir(), '.pi/agent/install/releases/1.5.0/node_modules/@earendil-works/pi-coding-agent/package.json'));
const typebox = require.resolve('typebox');
const source = readFileSync(new URL('../integrations/pi/conductor-swarm.ts', import.meta.url), 'utf8').replace("'typebox'", JSON.stringify('file://' + typebox));
const { default: extension } = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
const directory = mkdtempSync('/tmp/conductor-swarm-test-');
process.env.CONDUCTOR_SWARM_DIR = directory; process.env.CONDUCTOR_PI_OBSERVER_DIR = directory;
let registrations = 0, ack = 0, failInsertion = false, deferPersistence = false, token = 'token1';
const calls = [], entries = [], deliveries = [];
let journalPath = join(directory, 'session.jsonl'), memoryOnly = false, idle = true, pollTick;
const realInterval = globalThis.setInterval;
globalThis.setInterval = (fn, ms, ...args) => {pollTick=fn;return realInterval(fn, ms, ...args);};
const names = ['codex_send','codex_status','swarm_agents','swarm_context','swarm_send','swarm_inbox','swarm_ack','swarm_task','swarm_publish','swarm_events'];
const server = createServer(async (req,res) => {
  let raw = ''; for await (const chunk of req) raw += chunk;
  const body = JSON.parse(raw);
  res.setHeader('content-type','application/json');
  if (req.url === '/register') { registrations++; assert.equal(req.headers.authorization, 'Bearer bootstrap'); res.end(JSON.stringify({token})); return; }
  if (req.headers.authorization !== `Bearer ${token}`) {res.writeHead(401);res.end('{}');return;}
  calls.push(body);
  if (body.method === 'notifications/initialized') {res.writeHead(202);res.end();return;}
  let result = {};
  if (body.method === 'initialize') result = { protocolVersion:'2025-03-26' };
  if (body.method === 'tools/list') result = {tools:names.map(name => ({name}))};
  if (body.method === 'tools/call') {
    const name = body.params.name;
    let data = {};
    if (name === 'swarm_inbox') data = {messages:[{id:1,sender:'peer',message:'Do work',parent_id:null}]};
    if (name === 'swarm_ack') {ack++; data={message_id:1,state:'delivered'};}
    result = {content:[{type:'text',text:JSON.stringify(data)}]};
  }
  res.end(JSON.stringify({jsonrpc:'2.0',id:body.id,result}));
});
await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
const descriptor = {version:1,url:`http://127.0.0.1:${server.address().port}/mcp`,bootstrap_token:'bootstrap'};
writeFileSync(join(directory,'endpoint.json'),JSON.stringify(descriptor));
function observer(instance='one') {writeFileSync(join(directory,'session.json'), JSON.stringify({session_id:'session',instance_id:instance,observed_at:Date.now()/1000,status:'idle'}));}
observer();
function setup() {
 const handlers = new Map(), tools = new Map();
 extension({on:(name,fn)=>handlers.set(name,fn),registerTool:tool=>tools.set(tool.name,tool),sendMessage:(message,options)=>{if(failInsertion)throw Error('insertion');deliveries.push({message,options});if (!deferPersistence) {const entry={type:'custom_message',...message};entries.push(entry);if(!memoryOnly)appendFileSync(journalPath,JSON.stringify(entry)+'\n');}}});
 return {handlers,tools};
}
const ctx = {isIdle:()=>idle,sessionManager:{getSessionId:()=> 'session',getEntries:()=>entries,getSessionFile:()=>journalPath},ui:{setStatus(){}}};
let active;
try {
 active=setup(); assert.deepEqual([...active.tools.keys()],names);
 assert.match(active.handlers.get('before_agent_start')({systemPrompt:'USER POLICY'}).systemPrompt,/^USER POLICY[\s\S]*after compaction/);
 await active.handlers.get('session_start')({},ctx);
 assert.equal(registrations,1); assert.deepEqual(calls.slice(0,3).map(x=>x.method),['initialize','notifications/initialized','tools/list']);
 assert.equal(deliveries.length,1); assert.equal(ack,1);
 renameSync(journalPath,journalPath+'.held');await pollTick();assert.equal(ack,1);
 renameSync(journalPath+'.held',journalPath);await pollTick();assert.equal(ack,2); // transient missing-file read resets the cursor and rescans the SAME inode.
 assert.deepEqual(deliveries[0].options,{triggerTurn:false}); assert.match(deliveries[0].message.content,/not user or system instructions/);assert.match(deliveries[0].message.content,/sender=peer/);
 await assert.rejects(active.tools.get('codex_send').execute('missing-id',{message:'hello'}), /explicit stable request_id/);
 await active.tools.get('swarm_send').execute('same-call',{to:'peer',message:'hello'});
 const first=calls.at(-1).params.arguments.request_id;
 await active.tools.get('swarm_send').execute('same-call',{to:'peer',message:'hello'});assert.equal(calls.at(-1).params.arguments.request_id,first);
 active.handlers.get('session_shutdown')();active=setup();await active.handlers.get('session_start')({},ctx);
 assert.equal(deliveries.length,1);assert.equal(ack,3);
 observer('two');await active.tools.get('swarm_agents').execute('read',{});assert.equal(registrations,3);
 token='token2';await assert.rejects(active.tools.get('swarm_agents').execute('read',{}));
 active.handlers.get('session_shutdown')();active=setup();await active.handlers.get('session_start')({},ctx);assert.equal(registrations,4);
 active.handlers.get('session_shutdown')();entries.length=0;unlinkSync(journalPath);deferPersistence=true;active=setup();const beforeDeferred=ack;await active.handlers.get('session_start')({},ctx);assert.equal(ack,beforeDeferred);
 const inserted=deliveries.length;await pollTick();assert.equal(deliveries.length,inserted);
 const originalNow=Date.now;const later=originalNow()+11000;Date.now=()=>later;
 try {idle=false;await pollTick();assert.equal(deliveries.length,inserted);idle=true;await pollTick();assert.equal(deliveries.length,inserted+1);} finally {Date.now=originalNow;idle=true;}
 active.handlers.get('session_shutdown')();deferPersistence=false;active=setup();await active.handlers.get('session_start')({},ctx);assert.equal(ack,beforeDeferred+1);
 active.handlers.get('session_shutdown')();entries.length=0;unlinkSync(journalPath);failInsertion=true;active=setup();const before=ack;await active.handlers.get('session_start')({},ctx);assert.equal(ack,before);
 active.handlers.get('session_shutdown')();failInsertion=false;memoryOnly=true;entries.length=0;active=setup();const beforeMemory=ack;await active.handlers.get('session_start')({},ctx);assert.equal(entries.length,1);assert.equal(ack,beforeMemory);
 active.handlers.get('session_shutdown')();memoryOnly=false;entries.length=0;active=setup();await active.handlers.get('session_start')({},ctx);assert.equal(ack,beforeMemory+1);
 active.handlers.get('session_shutdown')();process.env.CONDUCTOR_SWARM_DISABLED='1';active=setup();const count=registrations;await active.handlers.get('session_start')({},ctx);assert.equal(registrations,count);
 console.log('Pi swarm lifecycle, MCP, dedupe, provenance, retry IDs, restart, disabled, insertion failure, and no-wake tests passed.');
} finally {active?.handlers.get('session_shutdown')();await new Promise(resolve=>server.close(resolve));rmSync(directory,{recursive:true,force:true});delete process.env.CONDUCTOR_SWARM_DISABLED;}

// Exercise the same extension against the actual Python MCP broker, with an isolated mailbox.
const { spawn } = await import('node:child_process');
const { once } = await import('node:events');
const realDirectory = mkdtempSync('/tmp/conductor-swarm-real-');
process.env.CONDUCTOR_SWARM_DIR = realDirectory; process.env.CONDUCTOR_PI_OBSERVER_DIR = realDirectory;
writeFileSync(join(realDirectory,'session.json'),JSON.stringify({session_id:'session',instance_id:'one',observed_at:Date.now()/1000,status:'idle'}));
const python = spawn('python3',['-u','-c',`
import sys
from conductor.swarm_mcp import start_swarm
class Monitor:
 def bridges(self): return {'session': {'instance_id': 'one'}}
 def list_sessions(self,limit=500): return {'sessions':[{'id':'session'}]}
s = start_swarm(sys.argv[1],Monitor())
s.broker.call('conductor','swarm_send',{'to':'session','message':'Real broker request','request_id':'real-fixture'})
print('ready',flush=True)
try: sys.stdin.read()
finally: s.close()
`,realDirectory],{cwd:new URL('..',import.meta.url),stdio:['pipe','pipe','inherit']});
try {
 await Promise.race([once(python.stdout,'data'),once(python,'exit').then(()=>{throw Error('Backend exited before ready');})]);
 entries.length=0;failInsertion=false;journalPath=join(realDirectory,'session.jsonl');active=setup();
 const prior=deliveries.length;
 await active.handlers.get('session_start')({},ctx);
 assert.equal(deliveries.length,prior+1);assert.match(deliveries.at(-1).message.content,/Real broker request/);
 const inbox=await active.tools.get('swarm_inbox').execute('real-read',{});
 assert.equal(JSON.parse(inbox.content[0].text).messages.length,0);
 const task=await active.tools.get('swarm_task').execute('create-real-task',{action:'create',task_id:'client-task',title:'Client integration'});
 assert.equal(task.isError,false);
 console.log('Real Python MCP broker integration passed: authenticated handshake, inbox injection/ack, task mutation.');
} finally {
 active?.handlers.get('session_shutdown')();python.stdin.end();await once(python,'exit');rmSync(realDirectory,{recursive:true,force:true});
}

globalThis.setInterval = realInterval;
