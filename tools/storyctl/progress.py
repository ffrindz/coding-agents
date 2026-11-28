"""The live progress page and status.json.

Each agent runs with a known Copilot session id, so its session log (~/.copilot/session-state/<id>/events.jsonl,
written while it works) can be read as it goes: what it reads, searches, runs and edits. progress.md is rewritten
at every change and stays open in an editor tab, which VS Code reloads by itself."""
import argparse, difflib, hashlib, json, os, re, shlex, shutil, signal, subprocess, sys, time, uuid
from .state import *
from .criteria import *
from .reports import *
from .repo import *
from .journal import *
from .activity import *

def set_status(state, **kw):
    st = {'state': state, 'pid': os.getpid(), 'updated': time.time(), 'credits': round(total_credits(), 1),
          'minutes': round(elapsed() / 60, 1), **kw}
    if G.LIVE and state == 'running':
        st['recent'] = activity_lines(G.LIVE['acts'])[-5:]; st['actions'] = len(G.LIVE['acts']); st['model'] = model_desc(G.LIVE.get('act'))
    tmp = os.path.join(G.S, 'status.json.tmp')
    json.dump(st, open(tmp, 'w')); os.replace(tmp, os.path.join(G.S, 'status.json'))
    try: write_progress(st)
    except (Exception, SystemExit) as e: print(f'[story-ctl] progress page not updated: {e!r}', file=sys.stderr)

def write_progress(st):
    try:
        meta_ = json.load(open(os.path.join(G.S, 'meta.json')))
    except (OSError, ValueError):
        return
    story = meta_.get('story_text', '')
    title = next((l.strip('#* ').strip() for l in story.splitlines() if l.strip()), 'story')
    state = st['state']
    head = {'running': f"Working: {step_name(st.get('step', ''))}", 'starting': 'Starting',
            'paused': f"Waiting for your reply at {st.get('where', 'a stop')}",
            'interrupted': 'Interrupted: reply `resume` in the chat to continue', 'ended': 'Finished'}.get(state, state)
    L = [f'# {_short(title, 90)}', '',
         f"**{head}** · {_cr(st.get('credits', 0))} · {st.get('minutes', 0):.0f} min so far · updated {time.strftime('%H:%M:%S')}", '']
    crit = parse_criteria(story)
    if crit:
        L += ['## Acceptance criteria', '', '```text', crit, '```', '']
    L += ['## Steps', '']
    open_start = None
    try: entries = load_journal(G.S)
    except SystemExit: entries = []             # a damaged journal is reported on resume, not here
    for e in entries:
        if e['type'] == 'start':
            if open_start and open_start is not G.LIVE: L.append(f"- ✗ {step_name(open_start['label'])}: ended without a report")
            open_start = e
        elif e['type'] == 'task':
            open_start = None
            f = e.get('file')
            mark = '✓' if not e.get('code') else f"⚠ exit code {e['code']}:"
            L.append(f"- {mark} {step_name(e['label'])}" + (f" · {e['model']}" if e.get('model') else '') + f" · {_mins(e.get('seconds', 0))} · {_cr(e.get('credits', 0))}"
                     + (f' · [report]({f})' if f else '') + (f" · [activity]({e['activity']})" if e.get('activity') else ''))
        elif e['type'] == 'reply':
            L.append(f"- ↩ your reply at {e['where']}: “{_short(e['text'] or '(empty)', 120)}”")
        elif e['type'] == 'end':
            L.append(f"- ■ {_short(e['result'], 200)} · [final report](final.md)")
    if G.LIVE:
        md = model_desc(G.LIVE.get('act'))
        L.append(f"- ▶ {step_name(G.LIVE['label'])}" + (f' · {md}' if md else '') + f" · running {_mins(time.time() - G.LIVE['since'])}")
    elif state == 'paused':
        L.append(f"- ⏸ waiting for your reply at {st.get('where')} · [open the stop]({os.path.basename(st.get('stop_file', ''))})")
    if G.LIVE:
        lines = activity_lines(G.LIVE['acts'])
        L += ['', f"## What {G.LIVE['agent']} is doing now" + (f' ({md})' if md else ''), '',
              f'{len(G.LIVE["acts"])} actions so far' + (f'; the last 25 below' if len(lines) > 25 else '') + '.', '', '```text']
        L += lines[-25:] or ['(starting)']
        L += ['```']
    _, out = git('status', '--porcelain=v1', '-uall')
    files = [l[3:] for l in out.decode(errors='replace').splitlines()]
    L += ['', '## Files changed in the working tree', ''] + ([f'- {x}' for x in files[:40]] or ['none'])
    if len(files) > 40: L.append(f'- … {len(files) - 40} more')
    L += ['', f'Run folder: {G.S}', '']
    tmp = os.path.join(G.S, 'progress.md.tmp')
    open(tmp, 'w').write('\n'.join(L)); os.replace(tmp, os.path.join(G.S, 'progress.md'))

__all__ = [n for n in dir() if not n.startswith('__')]
