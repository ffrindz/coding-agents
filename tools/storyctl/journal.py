"""Saved run files, the journal and resume, the run log, problem reports and statistics."""
import argparse, difflib, hashlib, json, os, re, shlex, shutil, signal, subprocess, sys, time, uuid
from .state import *
from .criteria import *
from .reports import *
from .repo import *

def save(name, text):
    open(os.path.join(G.S, name), 'w').write(text)
    return os.path.join(G.S, name)

def jwrite(entry):
    """The journal is written as the run goes, so an interrupted or stopped run can be resumed and reported."""
    with open(os.path.join(G.S, 'journal.jsonl'), 'a') as f:
        f.write(json.dumps(entry) + '\n')

def phash(p):
    return hashlib.sha256(p.encode()).hexdigest()[:16]

def credits_in(log):
    return sum(float(x) for x in re.findall(r'AI Credits\s+([\d.]+)', log))

def total_credits():
    return sum(G.usage.values())

def elapsed():
    return G.prev_seconds + (time.monotonic() - G.t0)

def versions():
    out = {}
    for x in G.trace:
        if x[0] == 'task':
            m = re.search(r'\((investigate|implement|review|explain)\s+(v[\d.]+)\)', x[3] or '')
            if m: out.setdefault(m.group(1), m.group(2))
    return out

def outcome(result):
    for pre, o in (('Approved', 'approved'), ('Not approved', 'not approved'), ('Needs answers after', 'not approved'),
                   ('Stopped early', 'not approved'), ('Stopped at', 'stopped by you'), ('Interrupted', 'interrupted')):
        if result.startswith(pre): return o
    return 'stopped (problem)'

def log_path():
    if G.a.no_log: return None
    return G.a.log or os.environ.get('STORY_CTL_LOG') or os.path.expanduser('~/.story-ctl/runs.jsonl')

def write_log(result):
    p = log_path()
    if not p: return
    os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    e = {'run': os.path.abspath(G.S), 'version': VERSION, 'started': G.meta.get('started'), 'ended': time.strftime('%Y-%m-%d %H:%M'),
         'repo': os.path.basename(G.ROOT), 'story': G.meta.get('story_name'), 'result': result.split('\n')[0][:200],
         'outcome': outcome(result), 'rounds': len(G.cur['verdicts']),
         'verdicts': [v.split(' ')[0] if not v.startswith('invalid') else 'invalid' for v in G.cur['verdicts']],
         'replies': sum(1 for x in G.trace if x[0] == 'user'), 'credits': {k: round(v, 2) for k, v in G.usage.items()},
         'credits_total': round(total_credits(), 2), 'minutes': round(elapsed() / 60, 1), 'agents': versions(),
         'resumed': G.meta.get('resumed', 0)}
    with open(p, 'a') as f:
        f.write(json.dumps(e) + '\n')

def replay_next(kind):
    """During --resume: the next saved entry of this kind, or None once the saved run is used up."""
    while G.replay_i < len(G.replay) and G.replay[G.replay_i]['type'] in ('start', 'session', 'pause'):
        G.replay_i += 1
    if G.replay_i >= len(G.replay): return None
    e = G.replay[G.replay_i]
    if e['type'] != kind:
        sys.exit(f'Cannot resume: the saved run has a {e["type"]} where this run expects a {kind}. Start a new run.')
    G.replay_i += 1
    return e

def resolve_run(x):
    if x != 'last': return x
    d = runs_dir()
    runs = sorted((p for p in (os.path.join(d, n) for n in (os.listdir(d) if os.path.isdir(d) else []))
                   if os.path.exists(os.path.join(p, 'meta.json'))), key=os.path.getmtime)
    if not runs: sys.exit(f'No saved runs in {d}.')
    return runs[-1]

def load_journal(d):
    """The journal's entries. A last line cut off by a kill is dropped (that entry never finished); a damaged
    line anywhere else stops with a message instead of guessing."""
    p = os.path.join(d, 'journal.jsonl')
    if not os.path.exists(p): return []
    lines = open(p, encoding='utf-8', errors='replace').read().split('\n')
    out = []
    for i, l in enumerate(lines):
        if not l.strip(): continue
        try: out.append(json.loads(l))
        except ValueError:
            if any(x.strip() for x in lines[i + 1:]):
                sys.exit(f'The journal of {d} is damaged at line {i + 1}; the run cannot be resumed safely. Start a new run.')
    return out

def prepare_resume(d):
    """Cut the saved journal back to the point where the run can go on. Returns (entries, interrupted step)."""
    j = load_journal(d)
    ends = [i for i, e in enumerate(j) if e['type'] == 'end']
    if ends:
        e = j[ends[-1]]
        if not e['resumable']:
            sys.exit(f'This run already ended ({e["result"].splitlines()[0]}); there is nothing to resume. Start a new run.')
        cut = ends[-1]
        if e['by_reply']:
            cut = max(i for i in range(cut) if j[i]['type'] == 'reply')
        j = j[:cut] + [x for x in j[cut:] if x['type'] == 'session']
    last_done = max([i for i, e in enumerate(j) if e['type'] in ('task', 'reply')] or [-1])
    starts = [e for e in j[last_done + 1:] if e['type'] == 'start']
    interrupted = starts[-1] if starts and not ends else None     # killed mid-step (no end written)
    tmp = os.path.join(d, 'journal.jsonl.tmp')
    with open(tmp, 'w') as f:
        for e in j: f.write(json.dumps(e) + '\n')
        f.flush(); os.fsync(f.fileno())
    shutil.copyfile(os.path.join(d, 'journal.jsonl'), os.path.join(d, 'journal.prev.jsonl'))
    os.replace(tmp, os.path.join(d, 'journal.jsonl'))
    return j, interrupted

def report(d):
    m = json.load(open(os.path.join(d, 'meta.json'))); j = load_journal(d)
    tasks = [e for e in j if e['type'] == 'task']
    vers = {}
    for t in tasks:
        v = re.search(r'\((investigate|implement|review|explain)\s+(v[\d.]+)\)', t['result'])
        if v: vers.setdefault(v.group(1), v.group(2))
    ends = [e for e in j if e['type'] == 'end']
    cr = sum(t['credits'] for t in tasks)
    L = ['# Problem report (story-ctl)', '',
         'Before sharing: replace system, service, field and people names with neutral ones (for example "Orders service",',
         '"field A"), but keep the shape: the wording of the criteria, your replies and the verdict lines. Then fill in the',
         'last three lines.', '', '```text',
         f'Agent and version:   ' + ', '.join(f'{k} {v}' for k, v in vers.items()) + f' (story-ctl v{m.get("version")})',
         'Model / effort:      (only if you changed the model lines in the agent files)',
         'Where:               Copilot CLI (story-ctl)',
         f'Result:              {ends[-1]["result"].splitlines()[0] if ends else "interrupted (no result)"}',
         f'Credits:             {cr:.0f} over {len(tasks)} agent runs', '',
         'Story shape (criteria as given):'] + ['  ' + l for l in parse_criteria(m['story_text']).splitlines()]
    L += ['', 'What the agents showed at each stop, and what you replied:']
    k = 0
    for e in j:
        if e['type'] == 'task':
            if e['label'] == 'explain-question':
                L.append(f'    explain answered: {re.sub(chr(10) + "+", " ", e["result"])[:300]}')
        if e['type'] == 'reply':
            k += 1
            asked = re.sub(r'\s+', ' ', e.get('asked') or '').strip() or '(none listed)'
            L.append(f'  Stop {k} ({e["where"]}): questions asked: {asked}')
            L.append(f'    you replied: {e["text"] or "(nothing)"}' + (f' [{e["failed"]}]' if e.get('failed') else ''))
    L += ['', "Each round's review:"]
    rn = 0
    for t in tasks:
        if t['label'].startswith('review-r'):
            rn += 1
            ed = field(t['result'], 'Edit')
            items = len([l for l in ed.splitlines() if re.match(r'\s*([-*]|\d+[.)])\s', l)]) or (0 if re.match(r'^(none|n/a)?\s*$', ed, re.I) else 1)
            L.append(f'  Round {rn}: {verdict_line(t["result"])}   Edit items: {items}')
    L += ['', 'What went wrong:', 'What you expected:', 'Did the code end up correct?   yes | no | not checked', '```']
    out = os.path.join(d, 'problem-report.md')
    open(out, 'w').write('\n'.join(L) + '\n')
    print(f'Problem report: {out}\nIt contains your story text and replies: replace names before sharing it.')

def stats():
    p = log_path()
    if not p or not os.path.exists(p): sys.exit('No run log yet.')
    runs = {}
    for l in open(p):
        try: e = json.loads(l)
        except ValueError: continue
        runs[e['run']] = e                      # a resumed run keeps its last line
    rs = list(runs.values())
    if not rs: sys.exit('No runs in the log.')
    n = len(rs); by = {}
    for e in rs: by[e['outcome']] = by.get(e['outcome'], 0) + 1
    ap_ = [e for e in rs if e['outcome'] == 'approved']
    avg = lambda xs: sum(xs) / len(xs) if xs else 0
    per = {}
    for e in rs:
        for k, v in e['credits'].items(): per.setdefault(k, []).append(v)
    print(f'{n} runs in {p}')
    print('  outcomes: ' + ', '.join(f'{k} {v}' for k, v in sorted(by.items(), key=lambda x: -x[1])))
    if ap_:
        print(f'  approved: {len(ap_)} of {n}; in round 1: {sum(1 for e in ap_ if e["rounds"] == 1)}; average rounds {avg([e["rounds"] for e in ap_]):.1f}')
    print(f'  credits per run: average {avg([e["credits_total"] for e in rs]):.0f} (' + ', '.join(f'{k} {avg(v):.0f}' for k, v in per.items()) + ')')
    print(f'  minutes per run: average {avg([e["minutes"] for e in rs]):.0f}; replies per run: {avg([e["replies"] for e in rs]):.1f}')
    print('  last runs:')
    for e in sorted(rs, key=lambda e: e.get('ended') or '')[-10:]:
        print(f'    {e.get("ended")}  {e["repo"]:<18.18} {e.get("story") or "":<22.22} {e["outcome"]:<16} rounds {e["rounds"]}  {e["credits_total"]:.0f} cr')

__all__ = [n for n in dir() if not n.startswith('__')]
