"""Trusted local Codex-to-Pi MCP for explicitly delegated work."""
import argparse
import json
from pathlib import Path
import sys

from . import conductor_mailbox, desktop_bridge, pi_router

S = {'type':'string'}
STRINGS = {'type':'array','items':{'type':'string','minLength':1,'maxLength':1000},'minItems':1,'maxItems':20}
PI_ID = {'type':'string','pattern':'^[A-Za-z0-9_-]{1,128}$','minLength':1,'maxLength':128}
CRITERIA = {'type':'array','items':{'type':'string','minLength':1,'maxLength':300},'minItems':1,'maxItems':20}
ROUTING_INSTRUCTIONS = (
    'Route data generation through Conductor and its Pi agents. Codex can execute platform work '
    '(including v2 platform operations), code changes, inspection, configuration, and verification '
    'directly. A data write is not automatically data generation. Use conductor_bypass for an '
    'explicit request-scoped routing decision; it never dispatches work or changes other chats. '
    'Do not duplicate in-flight delegated work. Routing does not change permissions or approvals.')


def bypass(args):
    operation = args.get('operation')
    if operation not in {'data_generation', 'platform', 'code', 'other'}:
        raise ValueError('operation must be data_generation, platform, code, or other')
    generation = operation == 'data_generation'
    return {'scope':'request', 'operation':operation, 'bypass':not generation,
            'route':'conductor' if generation else 'codex_direct', 'dispatched':False,
            'next_step':('Use conductor_send for data generation; Conductor manages Pi execution.' if generation else
                         'Execute this request using Codex tools; no Conductor or Pi handoff is required.'),
            'constraints':'Preserve existing permissions and approvals. Reconcile in-flight delegated work before taking it over.'}


def tool(name, description, properties, required=()):
    return {'name':name,'description':description,'inputSchema':{'type':'object','properties':properties,
        'required':list(required),'additionalProperties':False}}


TOOLS = [
    tool('conductor_bypass','Choose direct Codex execution for this request (for example v2 platform work). No dispatch, model call, or persistent/global mode change. Data generation still routes to Conductor. This is routing guidance, not a permissions override.',
         {'operation':{'type':'string','enum':['data_generation','platform','code','other']}},['operation']),
    tool('conductor_send','Give data generation or explicitly delegated work to Conductor’s own model, which manages Pi agents. Platform work, including v2, can execute directly in Codex. Reuse request_id on retries; omit chat_id to create a new Conductor chat. Queued waits for the app and model key. Do not independently dispatch the same work to Pi.',
         {'request_id':S,'chat_id':S,'message':{'type':'string','minLength':1,'maxLength':39000},
          'message_kind':{'type':'string','enum':['agent_feedback']},'task_key':S,'revision':S},['request_id','message']),
    tool('conductor_result','Read delivery and the manager’s reported result for an orchestration request. Reports require evidence review.',{'request_id':S},['request_id']),
    tool('pi_sessions','Discover exact Pi session IDs, live instances, context, and update state for optional delegation.',{}),
    tool('pi_context','Read bounded public Pi context to select the correct existing task/session.',{'session_id':S},['session_id']),
    tool('pi_dispatch','Delegate explicitly requested work to an exact Pi harness. This tool does not restrict direct Codex execution outside delegated work. Preserve unrelated Pi work. Busy Pi receives a follow-up. Reuse request_id on retries; accepted means delivery, not completion.',
         {'request_id':PI_ID,'session_id':PI_ID,'instance_id':PI_ID,'operation':{'type':'string','enum':['data_write','environment','other']},'task':{'type':'string','minLength':1,'maxLength':4000},'ownership':CRITERIA,'acceptance':CRITERIA},
         ['request_id','session_id','instance_id','operation','task','ownership','acceptance']),
    tool('pi_control','Steer or follow up with the exact existing active assignment owner, preserving its task and contract. Creates no task. Reuse control_id on retry; uncertain delivery requires reconciliation and is never replayed.',
         {'request_id':PI_ID,'control_id':PI_ID,'action':{'type':'string','enum':['steer','follow_up']},
          'message':{'type':'string','minLength':1,'maxLength':4000}},['request_id','control_id','action','message']),
    tool('pi_result','Read exact assignment delivery, Pi-reported result and Codex review. Completion requires review.',{'request_id':S},['request_id']),
    tool('pi_review','Record Codex acceptance after inspecting verification evidence for delegated Pi work.',
         {'request_id':S,'accepted':{'type':'boolean'},'evidence':STRINGS},['request_id','accepted','evidence']),
    tool('conductor_reply','Post a progress or final response to the original Conductor UI request. Only this trusted coordinator interface exposes replies; the authenticated Pi peer MCP does not expose this tool.',
         {'chat_id':S,'request_id':S,'response_id':S,'message':S,'status':{'type':'string','enum':['working','done','blocked']}},
         ['chat_id','request_id','response_id','message','status']),
    tool('conductor_status','Read hybrid manager/bridge configuration, recent chats, inbox counts, and routing policy: data generation goes to Conductor; platform/code work may execute directly in Codex.',{}),
]


def call(root, name, args):
    spec = next((tool['inputSchema'] for tool in TOOLS if tool['name'] == name), None)
    if not spec or not isinstance(args,dict) or set(args)-set(spec['properties']) or set(spec['required'])-set(args):
        raise ValueError('Unknown tool or invalid arguments')
    root = Path(root)
    if name == 'conductor_bypass':
        return bypass(args)
    if name == 'conductor_status':
        return desktop_bridge.status(root/'runs') | conductor_mailbox.status(root) | {
            'routing_policy':'data_generation_only', 'data_generation_route':'conductor',
            'platform_route':'codex_direct', 'bypass_scope':'request', 'routing_instructions':ROUTING_INSTRUCTIONS}
    if name == 'conductor_send':
        return conductor_mailbox.enqueue(root, args)
    if name == 'conductor_result':
        return conductor_mailbox.result(root, args['request_id'])
    if name == 'conductor_reply':
        return desktop_bridge.reply(root/'runs', args)
    if name == 'pi_sessions':
        return pi_router.sessions()
    if name == 'pi_context':
        return pi_router.context(args['session_id'])
    if name == 'pi_dispatch':
        return pi_router.dispatch(root/'swarm', args)
    if name == 'pi_control':
        return pi_router.control(root/'swarm', args)
    if name == 'pi_result':
        return pi_router.result(root/'swarm', args['request_id'])
    if name == 'pi_review':
        return pi_router.review(root/'swarm', args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.home()/'.local/share/conductor')
    parser.add_argument('--routing-policy', choices=['data-generation-only'], default='data-generation-only',
                        help='Data generation uses Conductor; platform/code work may run directly in Codex')
    parser.add_argument('--call', choices=[tool['name'] for tool in TOOLS], help='Call a tool once with JSON arguments on stdin')
    args = parser.parse_args()
    if args.call:
        try:
            print(json.dumps(call(args.root,args.call,json.load(sys.stdin)),ensure_ascii=False))
        except (ValueError,OSError) as exc:
            print(json.dumps({'error':str(exc)}))
            raise SystemExit(1)
        return
    ready = False
    for line in sys.stdin:
        ident = None
        try:
            body = json.loads(line)
            ident, method = body.get('id'), body.get('method')
            if method == 'notifications/initialized':
                ready = True
                continue
            if 'id' not in body:
                continue
            if method == 'initialize':
                ready = False
                result = {'protocolVersion':'2025-03-26','capabilities':{'tools':{}},
                          'instructions':ROUTING_INSTRUCTIONS,
                          'serverInfo':{'name':'conductor-pi','version':'1.1.0'}}
            elif method == 'ping':
                result = {}
            elif not ready:
                raise ValueError('Initialize first')
            elif method == 'tools/list':
                result = {'tools':TOOLS}
            elif method == 'tools/call':
                params = body.get('params',{})
                try:
                    value = call(args.root,params.get('name'),params.get('arguments',{}))
                    result = {'content':[{'type':'text','text':json.dumps(value,ensure_ascii=False)}], 'structuredContent':value,'isError':False}
                except (ValueError,OSError,TypeError) as exc:
                    result = {'content':[{'type':'text','text':str(exc)}],'isError':True}
            else:
                raise ValueError('Unknown method')
            response = {'jsonrpc':'2.0','id':ident,'result':result}
        except (ValueError,TypeError,AttributeError):
            response = {'jsonrpc':'2.0','id':ident,'error':{'code':-32600,'message':'Invalid MCP request'}}
        print(json.dumps(response),flush=True)


if __name__ == '__main__':
    main()
