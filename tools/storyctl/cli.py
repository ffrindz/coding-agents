"""Command line: options, starting or resuming a run."""
import argparse, difflib, hashlib, json, os, re, shlex, shutil, signal, subprocess, sys, time, uuid
from .state import *
from .criteria import *
from .reports import *
from .repo import *
from .journal import *
from .activity import *
from .progress import *
from .notify import *
from .agents import *
from .workflow import *
from .chat import *

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('story', nargs='?'); ap.add_argument('--base', default='HEAD'); ap.add_argument('--reply-cmd')
    ap.add_argument('--cap', type=int, default=300); ap.add_argument('--state')
    ap.add_argument('--agents', default='investigate,implement,review', help='agent names: investigate,implement,review')
    ap.add_argument('--explain-agent', default='explain', help='read-only agent that answers questions asked at a stop')
    ap.add_argument('--no-open', action='store_true', help='do not open each stop in VS Code')
    ap.add_argument('--resume', metavar='RUN', help='continue a stopped or interrupted run (its folder, or "last")')
    ap.add_argument('--report', metavar='RUN', help='write a problem report for a run (its folder, or "last")')
    ap.add_argument('--stats', action='store_true', help='summarise the run log')
    ap.add_argument('--log', help='run log file (default ~/.story-ctl/runs.jsonl, or $STORY_CTL_LOG)')
    ap.add_argument('--no-log', action='store_true', help='do not add this run to the run log')
    ap.add_argument('--step', action='store_true', help='chat mode: pause at each stop (exit 10) instead of waiting for input')
    ap.add_argument('--reply-file', help='chat mode, with --resume: the reply to the stop the run is paused at')
    ap.add_argument('--chat', action='store_true', help='the story agent\'s command: start or continue the story for this chat')
    ap.add_argument('--hook-prompt', action='store_true', help='the story agent\'s userPromptSubmitted hook')
    ap.add_argument('--hook-guard', action='store_true', help='the story agent\'s preToolUse hook')
    ap.add_argument('--fake', help='test mode: play back <agent>-<k>.md files from this folder instead of calling copilot')
    G.a = ap.parse_args(argv)
    G.ROOT = os.getcwd()
    G.reply_text = open(G.a.reply_file, encoding='utf-8', newline='').read() if G.a.reply_file else None     # exactly as typed
    if G.a.hook_prompt: return hook_prompt()
    if G.a.hook_guard: return hook_guard()
    G.fake_count, G.trace, G.answers, G.step, G.usage = {}, [], [], 0, {}
    G.cur = {'impl': None, 'rev': None, 'verdicts': [], 'handoff': None}
    G.t0, G.prev_seconds, G.replay, G.replay_i, G.pending, G.meta = time.monotonic(), 0.0, [], 0, None, {}
    if G.a.chat: return chat()
    if G.a.stats: return stats()
    if G.a.report: return report(resolve_run(G.a.report))
    if G.a.resume:
        G.S = resolve_run(G.a.resume)
        if not G.a.step:                          # the chat checks this itself before it resumes a run
            other, ost = active_run(exclude=G.S)
            if other: sys.exit(f'Another story in this repository is {ost["state"]} ({other}); finish or stop it first.')
        G.meta = json.load(open(os.path.join(G.S, 'meta.json')))
        G.replay, interrupted = prepare_resume(G.S)
        G.prev_seconds = sum(e['seconds'] for e in G.replay if e['type'] == 'session')
        for e in G.replay:
            if e['type'] == 'start': G.fake_count[e['agent']] = G.fake_count.get(e['agent'], 0) + 1
        G.pending = interrupted
        G.meta['resumed'] = G.meta.get('resumed', 0) + 1
        json.dump(G.meta, open(os.path.join(G.S, 'meta.json'), 'w'))
        story, base_desc = G.meta['story_text'], G.meta['base_desc']
        INV, IMP, REV = G.meta['agents']; G.a.explain_agent = G.meta['explain_agent']; G.a.cap = G.meta['cap']
        print(f'[story-ctl] resuming {G.S}' + (f' (the {interrupted["label"]} step was interrupted)' if interrupted else ''))
    else:
        if not G.a.story: ap.error('a story file is needed (or --resume, --report, --stats)')
        INV, IMP, REV = G.a.agents.split(',')
        story = open(G.a.story).read().strip()
        if not parse_criteria(story) or criteria_problem(story):
            why = criteria_problem(story)
            story, crit, _ = number_criteria(story)
            if why and crit: print(f'[story-ctl] {why}, so the criteria were numbered again; check them:', file=sys.stderr)
            if not crit:
                sys.exit('No acceptance criteria found in the story file: put them under an "Acceptance criteria" line, or start each with "AC1:", "AC2:", ...')
            story = story.strip()
            print('[story-ctl] acceptance criteria numbered from the story:\n' + crit, file=sys.stderr)
        rc, out = git('rev-parse', '--verify', '--quiet', G.a.base + '^{commit}')
        if rc:
            sys.exit(f'--base {G.a.base} is not a commit in this repository (run story-ctl from the repository root).')
        base_sha = out.decode().strip()
        base_desc = base_sha if G.a.base == base_sha else f'{base_sha} ({G.a.base} when the run started)'
        G.S = G.a.state or os.path.join(runs_dir(), new_run_id())
        os.makedirs(G.S, exist_ok=True)
        G.meta = {'version': VERSION, 'started': time.strftime('%Y-%m-%d %H:%M'), 'story_text': story,
                'story_name': os.path.basename(G.a.story), 'base_desc': base_desc, 'agents': [INV, IMP, REV],
                'explain_agent': G.a.explain_agent, 'cap': G.a.cap, 'resumed': 0}
        json.dump(G.meta, open(os.path.join(G.S, 'meta.json'), 'w'))
        if not G.a.step:                          # the chat checks this itself, under the same lock, before it starts a run
            with start_lock():
                other, ost = active_run(exclude=G.S)
                if other:
                    shutil.rmtree(G.S, ignore_errors=True)
                    sys.exit(f'Another story in this repository is {ost["state"]} ({other}). Finish or stop it first, '
                             'or use a second worktree (git worktree add) for a second story.')
                set_status('starting', step='starting')
        else:
            set_status('starting', step='starting')
        open_in_editor(os.path.join(G.S, 'progress.md'))
    criteria = parse_criteria(story)
    if not criteria:
        sys.exit('No acceptance criteria (lines starting "AC1:") found in the story file.')
    def _interrupt(signum, frame):
        raise KeyboardInterrupt
    for sig in (signal.SIGTERM, getattr(signal, 'SIGHUP', None), getattr(signal, 'SIGBREAK', None)):   # a closed terminal or a kill is handled like Ctrl-C
        if sig is not None: signal.signal(sig, _interrupt)
    signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        workflow(story, criteria, base_desc, INV, IMP, REV)
    except KeyboardInterrupt:
        jwrite({'type': 'session', 'seconds': round(time.monotonic() - G.t0)})
        write_log('Interrupted')
        try: last = json.load(open(os.path.join(G.S, 'status.json'))).get('step')
        except (OSError, ValueError): last = None
        set_status('interrupted', step=last or 'a step')
        print(f'\n[story-ctl] interrupted; everything so far is saved. To continue: --resume {G.S}', file=sys.stderr)
        sys.exit(130)

__all__ = [n for n in dir() if not n.startswith('__')]
