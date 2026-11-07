import argparse
from datetime import datetime
import getpass
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import uuid

from .demo import DemoProvider
from .provider import OpenRouter
from .runtime import cmux_binary, git, run_coordinator, run_worker
from .store import Store
from .models import DEFAULT_MODEL


def initialize(root, repo, goal, **options):
    root, repo = Path(root).resolve(), Path(repo).resolve()
    if not repo.is_dir():
        raise ValueError('Starting directory does not exist')
    if (root / 'state.sqlite').exists():
        raise ValueError('Run already exists; use resume or choose another --run path')
    base = None
    if options.get('allow_exec') and options.get('scope', 'repository') == 'repository':
        if git(repo, 'status', '--porcelain'):
            raise ValueError('Coding runs require a clean target Git checkout; commit or stash changes yourself first')
        base = git(repo, 'rev-parse', 'HEAD')
        if root.is_relative_to(repo):
            raise ValueError('Keep the run directory outside the target repository for coding runs')
    store = Store(root)
    config = dict(repo=str(repo), base=base, model=DEFAULT_MODEL, concurrency=3,
                  context_budget=24000, max_turns=0, max_tasks=12, max_calls=200,
                  allow_exec=False, transport='cmux', demo=False, scope='repository')
    config.update(options)
    store.configure(**config)
    store.add_agent('coordinator', None, goal)
    store.update_agent('coordinator', cwd=str(repo))
    store.event('user', '*', 'instruction', goal)
    (root / 'manifest.json').write_text(json.dumps(config | {'goal': goal}, indent=2) + '\n')
    return store


def provider_for(store, key=None):
    config = store.config()
    return DemoProvider() if config['demo'] else OpenRouter(config['model'], key=key, max_calls=config['max_calls'], state_path=store.root)


def positive(value):
    value = int(value)
    if value <= 0:
        raise argparse.ArgumentTypeError('Must be positive')
    return value


def turn_limit(value):
    value = int(value)
    if value < 0:
        raise argparse.ArgumentTypeError('Must be non-negative (0 means unlimited)')
    return value


def parser():
    p = argparse.ArgumentParser(description='Conductor: durable multi-agent context and cmux orchestration')
    sub = p.add_subparsers(dest='command', required=True)
    for name in ['run', 'demo']:
        command = sub.add_parser(name)
        command.add_argument('goal', nargs='?', default='Demonstrate three agents, dependency handoff, and conflicting context merge.' if name == 'demo' else None)
        command.add_argument('--repo', type=Path)
        command.add_argument('--filesystem', action='store_true', help='Work directly across your filesystem without Git worktrees')
        command.add_argument('--run', type=Path)
        command.add_argument('--transport', choices=['cmux', 'process'], default='process' if name == 'demo' else 'cmux')
        command.add_argument('--model', default=DEFAULT_MODEL)
        command.add_argument('--concurrency', type=positive, default=3)
        command.add_argument('--context-budget', type=positive, default=24000, help='Conservative input byte budget; output tokens reserved separately')
        command.add_argument('--max-turns', type=turn_limit, default=0, help='Agent turn limit; 0 means unlimited (default)')
        command.add_argument('--max-tasks', type=positive, default=12)
        command.add_argument('--max-calls', type=positive, default=200)
        command.add_argument('--allow-exec', action='store_true', help='Enable shell tools; filesystem mode edits actual files directly')
    for name in ['resume', 'chat', 'status', 'context', 'say', 'resolve', 'history']:
        command = sub.add_parser(name)
        command.add_argument('--run', required=name != 'chat', type=Path)
        if name == 'chat':
            command.add_argument('--workspace', type=Path, default=Path.home(), help='Starting directory for a new filesystem chat (default: home)')
            command.add_argument('--model', default=DEFAULT_MODEL)
            command.add_argument('--transport', choices=['cmux', 'process'], default='cmux')
            command.add_argument('--concurrency', type=positive, default=3)
        if name == 'say':
            command.add_argument('message', nargs='?')
            command.add_argument('--file', type=Path, help='Import user context from a UTF-8 text file')
        if name == 'resolve':
            command.add_argument('key')
            command.add_argument('value')
        if name == 'history':
            command.add_argument('--agent', default='coordinator')
            command.add_argument('--after', type=int, default=0)
        if name == 'resume':
            command.add_argument('--context-budget', type=positive)
    sub.add_parser('doctor')
    ui = sub.add_parser('ui-server', help=argparse.SUPPRESS)
    ui.add_argument('--port', type=int, default=0)
    ui.add_argument('--native', type=Path)
    ui.add_argument('--runtime-root', type=Path)
    worker = sub.add_parser('_worker', help=argparse.SUPPRESS)
    worker.add_argument('--run', required=True, type=Path)
    worker.add_argument('--agent', required=True)
    worker.add_argument('--socket', required=True)
    return p


def main(argv=None):
    os.umask(0o077)
    def terminate(signum, frame):
        raise KeyboardInterrupt('Terminated')
    signal.signal(signal.SIGTERM, terminate)
    args = parser().parse_args((sys.argv[1:] if argv is None else argv) or ['chat'])
    store = None
    try:
        if args.command == 'ui-server':
            from .ui_server import serve
            serve(args.port, args.native, args.runtime_root)
            return 0
        if args.command == 'doctor':
            print('Python:', sys.version.split()[0])
            print('OPENROUTER_API_KEY:', 'configured' if os.environ.get('OPENROUTER_API_KEY') else 'not configured')
            binary = cmux_binary()
            result = subprocess.run([binary, 'ping'], capture_output=True, text=True, timeout=10)
            print('cmux:', binary, result.stdout.strip() or result.stderr.strip())
            return 0 if result.returncode == 0 else 1
        if args.command == '_worker':
            run_worker(args.run, args.agent, args.socket)
            return 0
        if args.command in {'run', 'demo'}:
            if not args.goal:
                raise ValueError('Provide a goal')
            if args.context_budget < 12000:
                raise ValueError('Context budget must be at least 12000 bytes for the tool schema and pinned context')
            if args.command == 'run' and not os.environ.get('OPENROUTER_API_KEY'):
                raise ValueError('Set OPENROUTER_API_KEY in your environment first')
            root = args.run or Path.cwd() / '.conductor' / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
            repo = args.repo or (Path.home() if args.filesystem else Path.cwd())
            store = initialize(root, repo, args.goal, **{k: getattr(args, k) for k in
                ['model', 'concurrency', 'context_budget', 'max_turns', 'max_tasks', 'max_calls', 'allow_exec', 'transport']},
                demo=args.command == 'demo', scope='filesystem' if args.filesystem else 'repository')
            print('Run:', store.root, flush=True)
            run_coordinator(store, provider_for(store))
        else:
            if args.command == 'chat' and args.run is None:
                root = Path.home() / '.local' / 'share' / 'conductor' / 'runs' / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
                store = initialize(root, args.workspace, 'Help the user with their tasks across their filesystem. Follow the latest user request and preserve unrelated files.',
                    scope='filesystem', allow_exec=True, model=args.model, transport=args.transport, concurrency=args.concurrency)
            else:
                if not (args.run / 'state.sqlite').is_file():
                    raise ValueError('Run not found')
                store = Store(args.run)
            if args.command == 'say':
                if bool(args.message) == bool(args.file):
                    raise ValueError('Provide exactly one message or --file')
                message = args.file.read_text() if args.file else args.message
                if not message.strip():
                    raise ValueError('User context cannot be empty')
                event = store.event('user', '*', 'instruction', message)
                print(f'Saved user context #{event}; active agents receive it on their next turn. For an idle run, use resume.')
                return 0
            if args.command == 'resolve':
                store.resolve(args.key, args.value)
                print('Saved authoritative user decision for', args.key)
                return 0
            if args.command == 'status':
                print(json.dumps({'agents': store.agents(), 'usage': store.config().get('usage', {}), 'api_calls': store.config().get('api_calls', 0)}, indent=2))
                return 0
            if args.command == 'context':
                print(json.dumps({'user': store.user_context(), 'findings': store.claims(),
                    'checkpoints': {a['id']: store.checkpoint(a['id']) for a in store.agents()}}, indent=2))
                return 0
            if args.command == 'history':
                from .context import public_message
                print(json.dumps([{'id': i, 'message': public_message(m)} for i, m in store.history(args.agent, args.after)[:20]], indent=2))
                return 0
            if args.command == 'chat':
                config = store.config()
                key = None
                if not config['demo'] and not os.environ.get('OPENROUTER_API_KEY'):
                    key = getpass.getpass('OpenRouter key (hidden, kept only for this session): ').strip()
                    if not key:
                        raise ValueError('An OpenRouter key is required')
                print('Run:', store.root, flush=True)
                if config.get('scope') == 'filesystem':
                    print(f'Filesystem access · starts in {config["repo"]} · shell tools edit your actual files.')
                print('User context is pinned across all workers. /exit to quit. Use say from another terminal to steer during a run.')
                while True:
                    try:
                        message = input('you> ').strip()
                    except EOFError:
                        break
                    if message == '/exit':
                        break
                    if message:
                        store.event('user', '*', 'instruction', message)
                        run_coordinator(store, provider_for(store, key))
                return 0
            if args.command == 'resume':
                if args.context_budget:
                    store.configure(context_budget=args.context_budget)
                run_coordinator(store, provider_for(store))
        return 0 if store.agent('coordinator')['state'] == 'done' else 1
    except (Exception, KeyboardInterrupt) as exc:
        print('Conductor:', str(exc) or 'Interrupted', file=sys.stderr)
        return 1
    finally:
        if store:
            store.close()


if __name__ == '__main__':
    raise SystemExit(main())
