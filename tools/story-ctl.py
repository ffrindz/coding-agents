#!/usr/bin/env python3
"""story-ctl: run a whole story through investigate, implement and review, with you in the loop.

A small, deterministic workflow controller for the Copilot CLI. The three agents do the work; this script does
the bookkeeping that a model does unreliably: it passes every handoff and report on byte for byte, keeps all
your answers and gives them to every later task, reads the review verdict, counts rounds (at most 3), and stops
for you after the handoff and whenever a review has questions.

usage (from the root of the repository you are changing, for example in VS Code's terminal):
    python3 <path>/story-ctl.py story.txt [--base main]

story.txt holds the story with its acceptance criteria as lines starting "AC1:", "AC2:", ... (a criterion may
continue on indented or bulleted lines below it), or the story as copied from Jira: criteria under an
"Acceptance criteria" heading (bullets, numbers, Jira markup, Given/When/Then, a table, or one per line) are
numbered AC1:, AC2:, ... for you, without a model.
At each stop the agent's text is printed and opened in VS Code (press Cmd+Shift+V for the Markdown preview,
where the code links open the files). Type your reply and finish with an empty line:
  - "go" to continue, your answers, or "Change the plan: ..." at the handoff;
  - a question (ending with "?") is answered by the explain agent and you stay at the stop;
  - when implement cannot finish a round: "review", or an instruction for implement;
  - "stop" (or Ctrl-D with nothing typed) at any stop ends the run without changing anything more.
It stops early when a review asks for the same changes as the round before, and stops with a message when a
review report has no valid verdict, when an agent that must not edit (investigate, review, explain) changed
the working tree, or when implement failed after changing files (it never retries over partial edits and never
reverts your files).
Everything (prompts, reports, your replies, the final report) is saved under <git dir>/story-ctl/<time>/,
outside the working tree, so the agents never see it and nothing is committed.
Options: --base <branch> for review (default HEAD; resolved to a commit at the start), --cap <credits per agent
run> (default 300), --no-open.
Live view: progress.md in the run folder opens in an editor tab at the start and is rewritten as the run goes: the
criteria, each step with its time, credits and report, your replies, and what the running agent is reading,
searching, running (with pass or fail) and editing, from its Copilot session log; `status` in the chat shows the
last few actions. Each finished step's actions are kept in <step>.activity.md.
Notifications: at each stop and when the run ends, a desktop notification (macOS osascript, Windows PowerShell
toast, Linux notify-send; STORY_CTL_NOTIFY=0 turns them off), and the final report opens in an editor tab.
Run log: each run adds one line to ~/.story-ctl/runs.jsonl (on your machine only; --log FILE, $STORY_CTL_LOG,
or --no-log); --stats summarises it (outcomes, rounds, credits per agent, minutes). Credits and time so far are
shown at every stop and in the final report.
--resume RUN (a run folder, or "last") continues a run that was interrupted (Ctrl-C, a closed terminal), that you
stopped at a stop, or whose agent step failed twice (a service outage): finished steps and your replies are
replayed from the run's journal without calling the agents again; an interrupted implement step asks before it
runs again over its partial edits.
--report RUN writes problem-report.md for a run (criteria, each stop's questions and your reply, each round's
verdict line), ready to neutralise and share when an agent does something wrong.
Test options: --fake <folder> plays back canned agent replies (<agent>-<k>.md; optional <agent>-<k>.sh runs
first in the repository, <agent>-<k>.exit the exit code, <agent>-<k>.credits the credits, <agent>-<k>.events a
Copilot session log for the progress page); --reply-cmd answers stops from a command.
Tests: python3 tools/test_story_ctl.py (no Copilot, no credits).
"""
import argparse, difflib, hashlib, json, os, re, shutil, signal, subprocess, sys, time, uuid

VERSION = '1.7'
READ_ONLY_DENY = ['write', 'shell(git push)', 'shell(git commit)', 'shell(git reset)', 'shell(git checkout)',
                  'shell(git stash)', 'shell(git clean)', 'shell(git restore)']
WRITE_DENY = ['shell(git push)']

# ---------- pure helpers (imported by the tests) ----------

AC_RE = re.compile(r'^(\s*)(?:[-*]\s+)?(?:\*\*)?AC\d+(?:\*\*)?\s*:')

def parse_criteria(story):
    """Each AC line plus its continuation lines (indented deeper than the AC line, or bulleted under an
    unbulleted AC line), up to a blank line or the next AC."""
    out, cur_ind, cur_bullet = [], None, False
    for l in story.splitlines():
        m = AC_RE.match(l)
        if m:
            out.append(l.strip()); cur_ind = len(m.group(1)); cur_bullet = bool(re.match(r'\s*[-*]\s', l))
            continue
        if cur_ind is None:
            continue
        if not l.strip():
            cur_ind = None; continue
        ind = len(l) - len(l.lstrip())
        if ind > cur_ind or (not cur_bullet and re.match(r'\s*([-*]|\d+[.)])\s', l)):
            out.append(l.rstrip())
        else:
            cur_ind = None
    return '\n'.join(out)

# Acceptance criteria as copied from Jira (or anywhere): numbered as AC1:, AC2:, ... without a model.
AC_HEAD_RE = re.compile(r'^\s*(?:h[1-6]\.\s*|#{1,6}\s+)?[*_]{0,2}\s*(?:acceptance\s+criteri(?:a|on)s?|a\.?c\.?s?)\b(?!\s*[#-]?\s*\d)[^:\n]{0,30}?[*_]{0,2}\s*(?::\s*(.*))?$', re.I)
END_WORDS = (r'definition\s+of\s+(?:done|ready)|dod|dor|notes?|technical\s+notes|tech\s+notes|implementation\s+notes|'
             r'out\s+of\s+scope|non[- ]goals?|dependencies|assumptions|risks?|designs?|mock-?ups?|attachments?|'
             r'description|background|context|test(?:ing)?\s+notes|qa\s+notes|open\s+questions|questions|links?|'
             r'resources|story\s+points|scope|summary|user\s+story|story|tasks?|sub-?tasks?|comments?|activity')
END_RE = re.compile(r'^\s*(?:h[1-6]\.\s*.*|#{1,6}\s+[*_]{0,2}(?:' + END_WORDS + r')\b.*|[*_]{0,2}(?:' + END_WORDS + r')[*_]{0,2}\s*:?\s*[*_]{0,2})$', re.I)
LABEL_RE = re.compile(r'^\s*[*_]{0,2}(?:AC|acceptance\s+criteri(?:a|on)|criteri(?:a|on)|scenario|req(?:uirement)?)\s*[#-]?\s*\d+[*_]{0,2}\s*(?:[:.)\-–—]\s*[*_]{0,2}\s*|\s+)(.*)$', re.I)
MARK_RE = re.compile(r'^(\s*)(#+|\*+|[-+•◦▪▸►–—·]|\(?(?:\d{1,2}|[a-zA-Z])[.)])\s+(?:\[[ xX]\]\s+)?(.*\S)\s*$')
GWT_RE = re.compile(r'^\s*[*_]{0,2}(given|when|then|and|but)\b', re.I)

def _cells(l):
    l = l.strip()
    if '\t' in l: return [c.strip() for c in l.split('\t')], False
    head = l.startswith('||')
    return [c.strip() for c in re.split(r'\|\|?', l.strip('|'))], head

def _number(lines, heading):
    """Criteria (list of lists of lines: first line, then continuation lines) from the lines of an AC section."""
    lines = [l.rstrip() for l in lines]
    rows = [l for l in lines if l.strip()]
    if rows and all('\t' in l or l.strip().startswith('|') for l in rows) and len(rows) >= 2:
        cells = [_cells(l) for l in rows]
        hdr = None
        if cells[0][1] or all(len(c.split()) <= 3 and c for c in cells[0][0]):
            hdr = cells.pop(0)[0]
        out = []
        for c, _ in cells:
            if hdr and len(hdr) == len(c):
                out.append(['; '.join(f'{h}: {v}' for h, v in zip(hdr, c) if v)])
            else:
                out.append([' | '.join(v for v in c if v)])
        return [o for o in out if o[0]], []
    items, depth = [], []
    for l in lines:
        m = LABEL_RE.match(l)
        if m and m.group(1).strip():
            items.append(('label', 0, m.group(1).strip())); continue
        m = MARK_RE.match(l)
        if m:
            ind = len(m.group(1).expandtabs(4)); mk = m.group(2)
            d = ind + (len(mk) - 1) * 2 if mk[0] in '#*' else ind
            items.append(('mark', d, m.group(3).strip())); continue
        items.append(('blank', 0, '') if not l.strip() else ('plain', len(l) - len(l.lstrip()), l.strip()))
    marks = [d for k, d, _ in items if k == 'mark']
    labels = any(k == 'label' for k, _, _ in items)
    out, intro = [], []
    if marks or labels:
        top = min(marks) if marks else None
        for k, d, t in items:
            if k == 'label' or (k == 'mark' and d == top and not labels):
                out.append([t])
            elif k == 'mark' and out:
                out[-1].append('- ' + t)
            elif k == 'plain' and out:
                out[-1].append(t)
            elif k == 'plain':
                intro.append(t)                         # an intro line such as "The user can:"
        return out, intro
    texts = [t for k, _, t in items if k == 'plain']
    if any(GWT_RE.match(t) for t in texts):               # Given/When/Then scenarios without bullets
        for t in texts:
            w = GWT_RE.match(t); w = w.group(1).lower() if w else None
            steps = bool(out) and any(GWT_RE.match(x) for x in out[-1])
            if not out or (steps and w in (None, 'given')): out.append([t])
            else: out[-1].append(t)
        return out, []
    if heading or len(texts) >= 2:                       # one criterion per line (bullets lost in the copy)
        intro = texts[:1] if texts and texts[0].endswith(':') else []
        return [[t] for t in texts[len(intro):]], intro
    return [], []

def number_criteria(story):
    """(story with its acceptance criteria numbered "AC1:", "AC2:", ..., the numbered criteria, sure), or
    (story, '', False) when no criteria can be found. A story that already has AC lines is returned as it is.
    sure is False when the only evidence is plain lines with no heading, bullets or Given/When/Then: the
    developer confirms those before the run starts."""
    if parse_criteria(story):
        return story, parse_criteria(story), True
    lines = story.splitlines()
    start = next((i for i, l in enumerate(lines) if AC_HEAD_RE.match(l)), None)
    if start is not None:
        first = (AC_HEAD_RE.match(lines[start]).group(1) or '').strip()
        end = next((j for j in range(start + 1, len(lines)) if END_RE.match(lines[j]) and not AC_HEAD_RE.match(lines[j])), len(lines))
        crit, intro = _number(([first] if first else []) + lines[start + 1:end], True)
        before, after = (lines[:start + 1] if not first else lines[:start] + ['Acceptance criteria:']) + intro, lines[end:]
    else:
        crit, _ = _number(lines, False)
        if not crit:
            return story, '', False
        # keep any text before the first criterion (a title, a description) as it is
        firsttext = crit[0][0]
        k = next((i for i, l in enumerate(lines) if firsttext in l), 0)
        before, after = lines[:k], []
        if before and before[-1].strip(): before.append('')
        before.append('Acceptance criteria:')
    crit = [c for c in crit if c and c[0]]
    if not crit:
        return story, '', False
    block = []
    for n, c in enumerate(crit, 1):
        block.append(f'AC{n}: {c[0]}')
        block += ['  ' + x for x in c[1:]]
    tail = [''] + after if after else []
    new = '\n'.join(before + block + tail).rstrip() + '\n'
    plain = all(MARK_RE.match(l) is None and LABEL_RE.match(l) is None and GWT_RE.match(l) is None and '\t' not in l
                and not l.strip().startswith('|') for l in lines)
    return new, parse_criteria(new), start is not None or not plain

def strip_fences(r):
    return re.sub(r'^```.*?^```[^\n]*$', '', r, flags=re.M | re.S)

def verdict_check(r):
    """(verdict, problem). The verdict comes only from the single '- **Verdict:**' line outside code fences."""
    body = strip_fences(r)
    lines = re.findall(r'^\s*-\s*\*\*Verdict:\*\*[ \t]*(.*)$', body, re.M)
    if len(lines) != 1:
        return None, f'{len(lines)} verdict lines (expected exactly one)'
    m = re.match(r'(Approve|Request changes|Needs answers)(?=\s*(?:—|–|-|;|,|\.|$))', lines[0].strip())
    if not m:
        return None, f'unknown verdict "{lines[0].strip()[:60]}"'
    v = m.group(1)
    b = re.search(r'blockers:\s*(\d+)', lines[0])
    if v == 'Approve' and b and int(b.group(1)) > 0:
        return None, f'Approve with {b.group(1)} blocker(s)'
    return v, ''

def verdict(r):
    return verdict_check(r)[0]

def verdict_line(r):
    m = re.search(r'^\s*- \*\*Verdict:\*\*.*$', strip_fences(r), re.M)
    return m.group(0).strip() if m else '(no verdict line)'

def field(text, name):
    m = re.search(r'\*\*' + re.escape(name) + r'[^*]*:\*\*(.*?)(?=\n- \*\*|\nFor implement|\Z)', text, re.S)
    return m.group(1).strip() if m else ''

def has_questions(r):
    q = re.sub(r'\s+', ' ', field(r, 'Questions for the author or analyst'))
    return bool(q) and not re.match(r'^(none|n/a)\b', q, re.I)

def is_question(r):
    t = re.sub(r'\s+', ' ', r).strip()
    if not t or re.fullmatch(r'go\.?', t, re.I): return False
    parts = [p.strip() for p in re.findall(r'[^.!?]+[.!?]*', t) if p.strip()]
    return bool(parts) and all(p.endswith('?') for p in parts)

def is_stop(r):
    return not r.strip() or bool(re.fullmatch(r'\s*(stop|cancel|abort|quit)\.?\s*', r, re.I))

def is_go(r):
    return bool(re.fullmatch(r'\s*go\.?\s*', r, re.I))

def impl_incomplete(rep, exit_code=0):
    if exit_code:
        return f'the Copilot CLI exited with code {exit_code}'
    if 'Report (implement' not in rep: return 'the implement report is missing or cut off'
    m = re.search(r'circuit breaker|attempt limit|ask (?:the user|you) how to proceed|please advise how to proceed|could not (?:compile|run (?:the )?tests)|tests could not run|build (?:is )?(?:broken|failing)|BUILD FAILURE', rep, re.I)
    return m.group(0) if m else ''

def edit_norm(r):
    """The Edit field without links, line references, list numbers and formatting; other numbers are kept."""
    e = field(r, 'Edit')
    e = re.sub(r'\]\([^)]*\)', ']', e)
    e = re.sub(r'(?<=\w):\d+(?:-\d+)?', '', e)          # File.java:42-50
    e = re.sub(r'(?m)^\s*(?:[-*]|\d+[.)])\s+', ' ', e)  # list markers
    e = re.sub(r'[`*]', '', e)
    return re.sub(r'\s+', ' ', e).strip().lower()

def same_edits(prev, cur):
    """Same requested changes: at least 90% similar text and the same numbers (a changed limit is progress)."""
    if not (prev and cur) or sorted(re.findall(r'\d+', prev)) != sorted(re.findall(r'\d+', cur)):
        return False
    return difflib.SequenceMatcher(None, prev, cur).ratio() >= 0.9

# ---------- repository state ----------

def git(*args):
    p = subprocess.run(['git', *args], capture_output=True, text=False, cwd=ROOT)
    return p.returncode, p.stdout

def snapshot():
    """Hash of tracked changes (staged and unstaged) and untracked, non-ignored files; None outside git."""
    rc, _ = git('rev-parse', '--is-inside-work-tree')
    if rc: return None
    h = hashlib.sha256()
    for args in (('status', '--porcelain=v1', '-uall'), ('diff', '--binary', 'HEAD'), ('diff', '--binary', '--cached')):
        h.update(git(*args)[1])
    _, out = git('ls-files', '-o', '--exclude-standard', '-z')
    for f in sorted(x for x in out.decode(errors='replace').split('\0') if x):
        p = os.path.join(ROOT, f)
        if os.path.realpath(p).startswith(os.path.realpath(S) + os.sep): continue
        try: h.update(f.encode() + open(p, 'rb').read())
        except OSError: h.update(f.encode())
    return h.hexdigest()

def changed_files():
    _, out = git('status', '--porcelain=v1', '-uall')
    return '\n'.join(out.decode(errors='replace').splitlines()[:30]) or '(no files listed by git status; the staged or unstaged diff changed)'

# ---------- journal, run log, credits ----------

def save(name, text):
    open(os.path.join(S, name), 'w').write(text)
    return os.path.join(S, name)

def jwrite(entry):
    """The journal is written as the run goes, so an interrupted or stopped run can be resumed and reported."""
    with open(os.path.join(S, 'journal.jsonl'), 'a') as f:
        f.write(json.dumps(entry) + '\n')

def phash(p):
    return hashlib.sha256(p.encode()).hexdigest()[:16]

def credits_in(log):
    return sum(float(x) for x in re.findall(r'AI Credits\s+([\d.]+)', log))

def total_credits():
    return sum(usage.values())

def elapsed():
    return prev_seconds + (time.monotonic() - t0)

def versions():
    out = {}
    for x in trace:
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
    if a.no_log: return None
    return a.log or os.environ.get('STORY_CTL_LOG') or os.path.expanduser('~/.story-ctl/runs.jsonl')

def write_log(result):
    p = log_path()
    if not p: return
    os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    e = {'run': os.path.abspath(S), 'version': VERSION, 'started': meta.get('started'), 'ended': time.strftime('%Y-%m-%d %H:%M'),
         'repo': os.path.basename(ROOT), 'story': meta.get('story_name'), 'result': result.split('\n')[0][:200],
         'outcome': outcome(result), 'rounds': len(cur['verdicts']),
         'verdicts': [v.split(' ')[0] if not v.startswith('invalid') else 'invalid' for v in cur['verdicts']],
         'replies': sum(1 for x in trace if x[0] == 'user'), 'credits': {k: round(v, 2) for k, v in usage.items()},
         'credits_total': round(total_credits(), 2), 'minutes': round(elapsed() / 60, 1), 'agents': versions(),
         'resumed': meta.get('resumed', 0)}
    with open(p, 'a') as f:
        f.write(json.dumps(e) + '\n')

# ---------- running agents and stops ----------

def replay_next(kind):
    """During --resume: the next saved entry of this kind, or None once the saved run is used up."""
    global replay_i
    while replay_i < len(replay) and replay[replay_i]['type'] in ('start', 'session'):
        replay_i += 1
    if replay_i >= len(replay): return None
    e = replay[replay_i]
    if e['type'] != kind:
        sys.exit(f'Cannot resume: the saved run has a {e["type"]} where this run expects a {kind}. Start a new run.')
    replay_i += 1
    return e

def run_agent(agent, prompt, label, read_only):
    """One fresh copilot session; returns (final message, exit code). Retries once when nothing came back,
    unless the working tree changed during the failed attempt."""
    global step, pending, LIVE
    saved = replay_next('task')
    if saved is not None:
        if saved['agent'] != agent or saved['label'] != label or saved['phash'] != phash(prompt):
            sys.exit(f'Cannot resume: the saved {saved["label"]} step does not match this run ({label}). Start a new run.')
        step += saved['attempts']; usage[agent] = usage.get(agent, 0) + saved['credits']
        trace.append(['task', agent, prompt, saved['result'], [], saved['code']])
        print(f'[story-ctl] resumed: {label} (saved result)')
        return saved['result'], saved['code']
    if pending and pending['label'] == label and not pending['read_only']:
        r = ask(f'{agent} ({label}) was interrupted while it was running, so it may have left partial edits. Files changed '
                f'now:\n{changed_files()}\n\nReply "retry" to run that step again over the current files, or "stop".',
                'resume', 'the resume of an interrupted step', record=False)
        if not re.fullmatch(r'\s*(retry|go|resume|continue)\.?\s*', r, re.I):
            stop_with(f'Stopped at the resume of an interrupted step (reply was not "retry")', resumable=True)
    pending = None
    deny = READ_ONLY_DENY if read_only else WRITE_DENY
    spent, t_start = 0.0, time.monotonic()
    for attempt in (1, 2):
        LIVE = {'label': label, 'agent': agent, 'since': time.time(), 'acts': []}
        set_status('running', step=label, agent=agent)
        step += 1
        pf = save(f'{step:02d}-{label}.prompt.md', prompt)
        before = snapshot()
        final, tools, code, cr = '', [], 0, 0.0
        jwrite({'type': 'start', 'agent': agent, 'label': label, 'read_only': read_only})
        if a.fake:
            k = fake_count[agent] = fake_count.get(agent, 0) + 1
            base = os.path.join(os.path.abspath(a.fake), f'{agent}-{k}')
            if os.path.exists(base + '.events'):
                LIVE['act'] = Activity(base + '.events'); LIVE['acts'] = LIVE['act'].read(); set_status('running', step=label, agent=agent)
            if os.path.exists(base + '.sh'):
                subprocess.run(['sh', base + '.sh'], cwd=ROOT)
            if os.path.exists(base + '.exit'):
                code = int(open(base + '.exit').read().strip() or 0)
            if os.path.exists(base + '.credits'):
                cr = float(open(base + '.credits').read().strip() or 0)
            final = open(base + '.md').read().strip() if os.path.exists(base + '.md') else ''
            if not final: print(f'[story-ctl] fake: no {base}.md', file=sys.stderr)
        else:
            sid = str(uuid.uuid4())
            cmd = ['copilot', '--agent', agent, '-p', prompt, '--allow-all-tools', '--session-id', sid]
            for d in deny: cmd += ['--deny-tool', d]
            cmd += ['--max-ai-credits', str(a.cap), '--disable-client-telemetry']
            ev = os.path.expanduser(f'~/.copilot/session-state/{sid}/events.jsonl')
            act = Activity(ev); LIVE['acts'] = act.acts; LIVE['act'] = act
            lf = os.path.join(S, f'{step:02d}-{label}.stdout.txt')
            with open(lf, 'w') as out_f:
                p = subprocess.Popen(cmd, stdout=out_f, stderr=subprocess.STDOUT, cwd=ROOT)
                try:
                    seen, tick = 0, 0
                    while True:
                        try: p.wait(timeout=2); break
                        except subprocess.TimeoutExpired: pass
                        act.read(); tick += 1
                        if len(act.acts) != seen or tick % 10 == 0:
                            seen = len(act.acts); set_status('running', step=label, agent=agent)
                except BaseException:
                    p.kill(); p.wait(); raise
            code = p.returncode
            log = open(lf, encoding='utf-8', errors='replace').read()
            cr = credits_in(log)
            act.read()
            ev = ev if os.path.exists(ev) else None
            if not ev:
                old_sid = (re.findall(r'resume=([0-9a-f-]+)', log) or [None])[-1]
                ev = os.path.expanduser(f'~/.copilot/session-state/{old_sid}/events.jsonl') if old_sid else None
            if ev and os.path.exists(ev):
                for l in open(ev, encoding='utf-8', errors='replace'):
                    try: e = json.loads(l)
                    except ValueError: continue
                    d = e.get('data') or {}
                    if e.get('type') == 'assistant.message' and d.get('content'):
                        final = d['content']
                    if e.get('type') == 'tool.execution_start':
                        tools.append(d.get('toolName'))
            final = re.sub(r'\n*\[cost:[^\]]*\]\s*$', '', final).strip()
        spent += cr; usage[agent] = usage.get(agent, 0) + cr
        after = snapshot()
        touched = before is not None and after != before
        trace.append(['task', agent, prompt, final, tools, code])
        actf, mdesc = None, model_desc(LIVE.get('act')) if LIVE else ''
        if LIVE and LIVE['acts']:
            actf = f'{step:02d}-{label}.activity.md'
            save(actf, f'# What {agent} did ({step_name(label)})\n\n' + (f'Model: {mdesc}\n\n' if mdesc else '') + '```text\n' + '\n'.join(activity_lines(LIVE['acts'], short=False)) + '\n```\n')
        LIVE = None
        if final:
            save(f'{step:02d}-{label}.result.md', final)
            jwrite({'type': 'task', 'agent': agent, 'label': label, 'phash': phash(prompt), 'result': final, 'code': code,
                    'credits': spent, 'attempts': attempt, 'seconds': round(time.monotonic() - t_start),
                    'file': f'{step:02d}-{label}.result.md', 'activity': actf, 'model': mdesc})
        if read_only and touched:
            stop_with(f'Stopped — the {agent} step changed the working tree, but it must not edit. Nothing was '
                      f'reverted; check these files:\n{changed_files()}')
        if final:
            if code: print(f'[story-ctl] {agent} exited with code {code}', file=sys.stderr)
            return final, code
        if touched:
            stop_with(f'Stopped — the {agent} step returned no report after changing files, so it was not retried '
                      f'over the partial edits. Nothing was reverted; its output is in {S}. Changed files:\n{changed_files()}')
        print(f'\n[story-ctl] {agent} returned nothing (attempt {attempt}, exit code {code}); see {pf}', file=sys.stderr)
    stop_with(f'Stopped — the {agent} step failed twice; its output is in {S}. When the service is back, resume with '
              f'--resume {S}', resumable=True)

def ask(text, n, where, asked='', record=True):
    """Show a stop and get the developer's reply. An empty reply, end of input, "stop" or a failed reply
    command ends the run here (it can be resumed at this stop later). In chat mode (--step) the run pauses
    here instead, and the next chat message resumes it with --reply-file."""
    global reply_text
    if record:
        saved = replay_next('reply')
        if saved is not None:
            trace.append(['reply', text]); trace.append(['user', saved['text']])
            print(f'[story-ctl] resumed: your reply at {where} (saved)')
            return saved['text']
    text = text + f'\n\n(so far: {total_credits():.0f} credits, {elapsed() / 60:.0f} min)'
    shown = re.sub(r'\]\((?!https?:|/|#|mailto:)([^)\s]+)\)', lambda m: f']({ROOT}/{m.group(1)})', text)
    stop_file = save(f'stop-{n}.md', shown)
    failed = ''
    if reply_text is not None:                  # chat mode: the developer's message, captured by the hook
        r, reply_text = reply_text, None
        print(f'[story-ctl] your reply at {where} (from the chat)')
    elif a.step:                                # chat mode: pause; the next chat message resumes the run
        open_in_editor(stop_file); notify(stop_note(where))
        jwrite({'type': 'session', 'seconds': round(time.monotonic() - t0)})
        set_status('paused', where=where, stop_file=stop_file)
        print(f'[story-ctl] paused at {where}: {stop_file}')
        sys.exit(10)
    else:
        print('\n' + '=' * 78 + '\n' + text + '\n' + '=' * 78)
        print(f'[story-ctl] this stop is also in {stop_file} (Markdown preview: Cmd+Shift+V; links open the code)')
        if a.reply_cmd:
            p = subprocess.run(a.reply_cmd.format(stop_file=stop_file, n=n), shell=True, capture_output=True, text=True)
            r = p.stdout.strip()
            if p.returncode: failed = f'the reply command exited with code {p.returncode}'
            print(f'[reply] {r}')
        else:
            open_in_editor(stop_file); notify(stop_note(where).replace('in the chat', 'in the terminal'))
            print('Your reply (empty line to finish; "stop" to end the run):')
            lines = []
            for l in sys.stdin:
                if not l.strip() and lines: break
                lines.append(l.rstrip('\n'))
            r = '\n'.join(lines).strip()
    trace.append(['reply', text]); trace.append(['user', r]); save(f'reply-{n}.md', r)
    if record:
        jwrite({'type': 'reply', 'where': where, 'asked': asked, 'text': r, 'failed': failed})
    if record and (failed or is_stop(r)):
        why = failed or ('no reply' if not r.strip() else 'you')
        stop_with(f'Stopped at {where} ({why}); nothing further was changed. To continue from this stop later: '
                  + ('reply resume in the chat' if a.step else f'--resume {S}'), resumable=True, by_reply=True)
    return r

WIN_TOAST = (r"$ErrorActionPreference='Stop';"
             r"[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]|Out-Null;"
             r"$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
             r"$t=$x.GetElementsByTagName('text');$t.Item(0).AppendChild($x.CreateTextNode($env:STORY_CTL_N_TITLE))|Out-Null;"
             r"$t.Item(1).AppendChild($x.CreateTextNode($env:STORY_CTL_N_TEXT))|Out-Null;"
             r"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe').Show([Windows.UI.Notifications.ToastNotification]::new($x))")

def story_title():
    try: story = json.load(open(os.path.join(S, 'meta.json'))).get('story_text', '')
    except (OSError, ValueError): story = ''
    return _short(next((l.strip('#* ').strip() for l in story.splitlines() if l.strip()), 'story'), 60)

def notify(text):
    """A desktop notification (macOS: osascript; Windows: a PowerShell toast; Linux: notify-send). Never fails the
    run. STORY_CTL_NOTIFY=0 turns it off; STORY_CTL_NOTIFY_CMD runs a command of your own with the title and text."""
    if os.environ.get('STORY_CTL_NOTIFY', '1').strip().lower() in ('0', 'off', 'no', 'false'): return
    title = 'Story: ' + story_title()
    env = dict(os.environ, STORY_CTL_N_TITLE=title, STORY_CTL_N_TEXT=text)
    own = os.environ.get('STORY_CTL_NOTIFY_CMD')
    if own: cmd = [*own.split(), title, text]
    elif sys.platform == 'darwin':
        cmd = ['osascript', '-e', 'on run argv', '-e', 'display notification (item 2 of argv) with title (item 1 of argv)',
               '-e', 'end run', title, text]
    elif os.name == 'nt': cmd = ['powershell', '-NoProfile', '-NonInteractive', '-Command', WIN_TOAST]
    elif shutil.which('notify-send'): cmd = ['notify-send', title, text]
    else: return
    try: subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    except (OSError, subprocess.SubprocessError): pass

def stop_note(where):
    if where == 'the handoff': return 'The plan is ready (in an editor tab). Reply in the chat: go, your answers, or stop.'
    m = re.match(r'the round (\d) review questions', where)
    if m: return f'The round {m.group(1)} review has questions (in an editor tab). Reply in the chat.'
    if 'implement' in where: return 'Implement could not finish a round (in an editor tab). Reply in the chat: review, an instruction, or stop.'
    return f'Waiting for your reply at {where} (in an editor tab). Reply in the chat.'

def open_in_editor(path):
    if a.no_open: return
    ed = os.environ.get('STORY_CTL_EDITOR')
    ed = (ed.split() if ' ' in ed and not os.path.exists(ed) else [ed]) if ed else ([shutil.which('code')] if shutil.which('code') else None)
    if ed:
        try: subprocess.run([*ed, '-r', path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        except (OSError, subprocess.SubprocessError): pass

def set_status(state, **kw):
    st = {'state': state, 'pid': os.getpid(), 'updated': time.time(), 'credits': round(total_credits(), 1),
          'minutes': round(elapsed() / 60, 1), **kw}
    if LIVE and state == 'running':
        st['recent'] = activity_lines(LIVE['acts'])[-5:]; st['actions'] = len(LIVE['acts']); st['model'] = model_desc(LIVE.get('act'))
    tmp = os.path.join(S, 'status.json.tmp')
    json.dump(st, open(tmp, 'w')); os.replace(tmp, os.path.join(S, 'status.json'))
    try: write_progress(st)
    except Exception as e: print(f'[story-ctl] progress page not updated: {e}', file=sys.stderr)

def answers_block():
    if not answers: return ''
    return ('\n\nAnswers from the user (verbatim; a later answer replaces an earlier one on the same point):\n'
            + '\n'.join(f'{lab}:\n{txt}' for lab, txt in answers))

def answer_question(q, context, what):
    ans, _ = run_agent(a.explain_agent, f'A developer reviewing this {what} asks (verbatim): {q}\n\nAnswer the question from the code and the {what} below. Do not change anything, and do not plan new work.\n\n{what.capitalize()}:\n{context}', 'explain-question', True)
    return ans

def stop_with(result, resumable=False, by_reply=False):
    impl, rev, rounds = cur['impl'], cur['rev'], cur['verdicts']
    by = ', '.join(f'{k} {v:.0f}' for k, v in usage.items())
    rep = [f'# Story (story-ctl v{VERSION})', f'- **Result:** {result}',
           f'- **Rounds:** {len(rounds)} implement + review rounds; ' + '; '.join(f'round {i+1}: {v}' for i, v in enumerate(rounds)),
           f'- **Credits and time:** {total_credits():.0f} credits ({by or "none"}); {elapsed() / 60:.0f} min']
    if rev:
        rep += ['- **Criteria:**\n' + field(rev, 'Criteria'),
                '- **Open items:**', '  - Findings: ' + (field(rev, 'Findings') or 'none'),
                '  - Questions: ' + (field(rev, 'Questions for the author or analyst') or 'none'),
                '  - Not verified / risks: ' + (field(rev, 'Not verified / risks') or 'none')]
    hq = field(cur['handoff'], 'Questions for the analyst') if cur['handoff'] else ''
    if hq and not re.match(r'^(none|n/a)\b', hq, re.I):
        rep += ['  - Handoff questions (check that each was answered, or that its assumption is what you want):\n' + hq]
    rep += ['  - Your answers so far:' + (answers_block() or ' none')]
    if impl: rep += ['\n## Last implement report\n', impl]
    if rev: rep += ['\n## Last review\n', rev]
    final = '\n'.join(rep)
    save('final.md', final); trace.append(['reply', final])
    json.dump(trace, open(os.path.join(S, 'trace.json'), 'w'))
    jwrite({'type': 'end', 'result': result, 'resumable': resumable, 'by_reply': by_reply})
    jwrite({'type': 'session', 'seconds': round(time.monotonic() - t0)})
    write_log(result)
    set_status('ended', result=result, resumable=resumable, final=os.path.join(S, 'final.md'))
    if not by_reply:                            # you stopped it yourself: no need to tell you
        open_in_editor(os.path.join(S, 'final.md'))
        notify(_short(result.splitlines()[0], 120).rstrip('.') + '. The final report is open in an editor tab.')
    print('\n' + final + f'\n\n[story-ctl] everything is saved in {S}; a problem report for this run: --report {S}')
    sys.exit(0)

# ---------- live progress page (progress.md in the run folder) ----------
#
# Each agent runs with a known Copilot session id, so its session log (~/.copilot/session-state/<id>/events.jsonl,
# written while it works) can be read as it goes: what it reads, searches, runs and edits. progress.md is rewritten
# at every change and stays open in an editor tab, which VS Code reloads by itself.

LIVE = None                 # the agent step running now: {'label', 'agent', 'since', 'acts'}
ACT_SKIP = {'report_intent', 'think', 'update_todo', 'read_bash', 'list_bash', 'read_powershell', 'list_powershell'}

def _rel(p):
    p = str(p or '')
    root = globals().get('ROOT') or ''
    try: return os.path.relpath(p, root) if root and os.path.isabs(p) and p.startswith(root + os.sep) else p
    except ValueError: return p

def _short(s, n=110):
    s = ' '.join(str(s).split())
    return s if len(s) <= n else s[:n - 1] + '…'

def describe_tool(name, args):
    if name == 'apply_patch':
        files = re.findall(r'^\*\*\* (?:Update|Add|Delete) File: (.+)$', args if isinstance(args, str) else json.dumps(args), re.M)
        return ['edit', ', '.join(_rel(f.strip()) for f in files) or 'a patch', '']
    g = args if isinstance(args, dict) else {}
    if name in ('view', 'read', 'read_file'): return ['read', _rel(g.get('path')), '']
    if name in ('rg', 'grep', 'search'): return ['search', _short(g.get('pattern') or g.get('query') or '', 70), '']
    if name == 'glob': return ['find', _short(g.get('pattern', ''), 70), '']
    if name in ('bash', 'powershell', 'shell'):
        c = (g.get('command') or '').strip().splitlines()
        return ['run', _short(c[0] if c else '', 90), '']
    if name in ('edit', 'str_replace', 'str_replace_editor', 'write'): return ['edit', _rel(g.get('path')), '']
    if name == 'create': return ['create', _rel(g.get('path')), '']
    if name == 'skill': return ['skill', _short(g.get('skill') or g.get('name') or '', 60), '']
    return [name, _short(json.dumps(g), 60) if g else '', '']

class Activity:
    """Reads a Copilot session log as it grows (a line still being written is kept for the next read)."""
    def __init__(self, path):
        self.path, self.pos, self.buf, self.acts, self.idx = path, 0, '', [], {}
        self.model, self.effort, self.used = None, None, []        # used: model names in the order they answered
    def read(self):
        try:
            with open(self.path, encoding='utf-8', errors='replace') as f:
                f.seek(self.pos); data = f.read(); self.pos = f.tell()
        except OSError:
            return self.acts
        lines = (self.buf + data).split('\n'); self.buf = lines.pop()
        for l in lines:
            try: e = json.loads(l)
            except ValueError: continue
            t, d = e.get('type'), e.get('data') or {}
            if t == 'session.model_change' and d.get('newModel'):
                self.model, self.effort = d['newModel'], d.get('reasoningEffort') or self.effort
            elif t == 'session.usage_record':
                u = d.get('usage') or {}
                name = u.get('modelDisplayName') or u.get('model')
                if name and u.get('model') != 'none':
                    self.model, self.effort = name, u.get('reasoningEffort') or self.effort
                    if name not in self.used: self.used.append(name)
            if t == 'assistant.message' and d.get('toolRequests') and (d.get('content') or '').strip():
                self.acts.append(['note', _short(d['content'], 150), ''])
            elif t == 'tool.execution_start' and d.get('toolName') not in ACT_SKIP:
                self.idx[d.get('toolCallId')] = len(self.acts)
                self.acts.append(describe_tool(d.get('toolName', ''), d.get('arguments')))
            elif t == 'tool.execution_complete' and d.get('toolCallId') in self.idx:
                x = self.acts[self.idx.pop(d['toolCallId'])]
                code = (d.get('shellExecution') or {}).get('exitCode')
                if x[0] == 'run' and code is not None: x[2] = '✓' if code == 0 else f'✗ exit {code}'
                elif d.get('success') is False: x[2] = '✗ failed'
        return self.acts

def model_desc(act):
    """"GPT-6.1 Sol, effort medium", or "GPT-6.1 Sol → GPT-6 Luna, effort medium" after a fallback."""
    if not act or not (act.used or act.model): return ''
    name = ' → '.join(act.used) if act.used else act.model
    return name + (f', effort {act.effort}' if act.effort else ', default effort')

def activity_lines(acts, short=True):
    """One line per action; consecutive reads, searches and finds are put on one line. short: file names only
    for reads and edits (the activity file of a finished step keeps the paths)."""
    out = []
    for kind, what, res in acts:
        if short and kind in ('read', 'edit', 'create'):
            what = ', '.join(os.path.basename(x.strip()) or x for x in what.split(', '))
        if out and kind in ('read', 'search', 'find') and out[-1][0] == kind and not out[-1][2] and not res and len(out[-1][1]) < 100:
            out[-1][1] += ', ' + what; continue
        out.append([kind, what, res])
    return [f'{k:<7} {w}' + (f'   {r}' if r else '') for k, w, r in out]

def step_name(label):
    m = re.match(r'(implement|review)-r(\d)$', label)
    if m: return f'{m.group(1)} round {m.group(2)}'
    return {'explain-question': 'explain (your question)', 'reinvestigate': 'investigate again (your changes)'}.get(label, label)

def _mins(s): return f'{s / 60:.0f} min' if s >= 60 else f'{s:.0f} s'

def _cr(x): return f'{x:.1f} credits' if x < 10 else f'{x:.0f} credits'

def write_progress(st):
    try:
        meta_ = json.load(open(os.path.join(S, 'meta.json')))
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
    for e in load_journal(S):
        if e['type'] == 'start':
            if open_start and open_start is not LIVE: L.append(f"- ✗ {step_name(open_start['label'])}: ended without a report")
            open_start = e
        elif e['type'] == 'task':
            open_start = None
            f = e.get('file')
            L.append(f"- ✓ {step_name(e['label'])}" + (f" · {e['model']}" if e.get('model') else '') + f" · {_mins(e.get('seconds', 0))} · {_cr(e.get('credits', 0))}"
                     + (f' · [report]({f})' if f else '') + (f" · [activity]({e['activity']})" if e.get('activity') else ''))
        elif e['type'] == 'reply':
            L.append(f"- ↩ your reply at {e['where']}: “{_short(e['text'] or '(empty)', 120)}”")
        elif e['type'] == 'end':
            L.append(f"- ■ {_short(e['result'], 200)} · [final report](final.md)")
    if LIVE:
        md = model_desc(LIVE.get('act'))
        L.append(f"- ▶ {step_name(LIVE['label'])}" + (f' · {md}' if md else '') + f" · running {_mins(time.time() - LIVE['since'])}")
    elif state == 'paused':
        L.append(f"- ⏸ waiting for your reply at {st.get('where')} · [open the stop]({os.path.basename(st.get('stop_file', ''))})")
    if LIVE:
        lines = activity_lines(LIVE['acts'])
        L += ['', f"## What {LIVE['agent']} is doing now" + (f' ({md})' if md else ''), '',
              f'{len(LIVE["acts"])} actions so far' + (f'; the last 25 below' if len(lines) > 25 else '') + '.', '', '```text']
        L += lines[-25:] or ['(starting)']
        L += ['```']
    _, out = git('status', '--porcelain=v1', '-uall')
    files = [l[3:] for l in out.decode(errors='replace').splitlines()]
    L += ['', '## Files changed in the working tree', ''] + ([f'- {x}' for x in files[:40]] or ['none'])
    if len(files) > 40: L.append(f'- … {len(files) - 40} more')
    L += ['', f'Run folder: {S}', '']
    tmp = os.path.join(S, 'progress.md.tmp')
    open(tmp, 'w').write('\n'.join(L)); os.replace(tmp, os.path.join(S, 'progress.md'))

# ---------- resume, report and stats ----------

def new_run_id():
    return time.strftime('%Y%m%d-%H%M%S') + '-' + os.urandom(2).hex()

def runs_dir():
    _, gd = git('rev-parse', '--absolute-git-dir')
    return os.path.join(gd.decode().strip() or os.path.join(ROOT, '.git'), 'story-ctl')

def resolve_run(x):
    if x != 'last': return x
    d = runs_dir()
    runs = sorted((p for p in (os.path.join(d, n) for n in (os.listdir(d) if os.path.isdir(d) else []))
                   if os.path.exists(os.path.join(p, 'meta.json'))), key=os.path.getmtime)
    if not runs: sys.exit(f'No saved runs in {d}.')
    return runs[-1]

def load_journal(d):
    p = os.path.join(d, 'journal.jsonl')
    return [json.loads(l) for l in open(p)] if os.path.exists(p) else []

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
    os.replace(os.path.join(d, 'journal.jsonl'), os.path.join(d, 'journal.prev.jsonl'))
    with open(os.path.join(d, 'journal.jsonl'), 'w') as f:
        for e in j: f.write(json.dumps(e) + '\n')
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

# ---------- chat mode: the story agent in the VS Code agent window (or Copilot CLI) ----------
#
# The story agent runs one command, `story-ctl.py --chat`, for every message and shows its output. Your message
# reaches the controller through the agent's userPromptSubmitted hook (--hook-prompt), never through the model;
# its preToolUse hook (--hook-guard) denies every other command. --chat starts or continues the run in the
# background and returns at once; each stop opens in an editor tab, and your next chat message is the reply.

CHAT_HELP = ('Paste a story with its acceptance criteria to start (straight from Jira is fine: the criteria are numbered '
             'AC1:, AC2:, ... for you). At each stop '
             'the plan or review opens in an editor tab; reply here with `go`, your answers, a question ending in `?`, '
             'or `stop`. Other messages: `status`, `resume` (continue the last stopped run in this repository), '
             '`stats`, `report`.')
CHAT_CMD = re.compile(r'''^\s*(?:cd\s+\S+\s*(?:&&|;)\s*)?(?:python3?|py)(?:\s+-I)?\s+(?:"[^"]*story-ctl\.py"|'[^']*story-ctl\.py'|[^"'\s;&|<>`$()]*story-ctl\.py|"\$env:[^"]*story-ctl\.py")\s+--chat\s*(?:2>&1)?\s*$''')
READ_ONLY_TOOLS = {'read_bash', 'list_bash', 'get_terminal_output', 'read_powershell', 'list_powershell'}

def home():
    return os.environ.get('STORY_CTL_HOME') or os.path.expanduser('~/.story-ctl')

def hook_input():
    try: return json.loads(sys.stdin.read() or '{}')
    except ValueError: return {}

def hook_prompt():
    """userPromptSubmitted hook of the story agent: keep the developer's exact message for --chat."""
    d = hook_input()
    sid = re.sub(r'[^\w.-]', '_', str(d.get('sessionId') or d.get('session_id') or 'unknown'))
    prompt = d.get('prompt') if 'prompt' in d else d.get('user_prompt')
    if prompt is None: return
    os.makedirs(os.path.join(home(), 'inbox'), exist_ok=True)
    tmp = os.path.join(home(), 'inbox', sid + '.tmp')
    json.dump({'sessionId': sid, 'prompt': prompt, 'cwd': d.get('cwd') or os.getcwd(), 'ts': time.time()}, open(tmp, 'w'))
    os.replace(tmp, os.path.join(home(), 'inbox', sid + '.json'))

def hook_guard():
    """preToolUse hook of the story agent: allow only the story-ctl --chat command."""
    d = hook_input()
    name = str(d.get('toolName') or d.get('tool_name') or '')
    args = d.get('toolArgs') if 'toolArgs' in d else d.get('tool_input')
    if isinstance(args, str):
        try: args = json.loads(args)
        except ValueError: args = {'command': args}
    cmd = (args or {}).get('command') if isinstance(args, dict) else None
    if name in READ_ONLY_TOOLS or (isinstance(cmd, str) and CHAT_CMD.match(cmd)):
        print('{}'); return
    print(json.dumps({'permissionDecision': 'deny', 'permissionDecisionReason':
                      'The story agent may only run the story-ctl --chat command shown in its instructions.'}))

def read_message():
    """(session id, message, message time) from the hook's inbox, else from the Copilot session log."""
    sid = os.environ.get('COPILOT_AGENT_SESSION_ID') or ''
    inbox = os.path.join(home(), 'inbox')
    cand = []
    if sid and os.path.exists(os.path.join(inbox, sid + '.json')):
        cand = [os.path.join(inbox, sid + '.json')]
    elif os.path.isdir(inbox):
        here = os.path.realpath(ROOT)
        for f in os.listdir(inbox):
            if not f.endswith('.json'): continue
            p = os.path.join(inbox, f)
            try: e = json.load(open(p))
            except ValueError: continue
            c = os.path.realpath(e.get('cwd') or '')
            if (here == c or here.startswith(c + os.sep) or c.startswith(here + os.sep)) and time.time() - e['ts'] < 600:
                cand.append(p)
    if cand:
        e = json.load(open(max(cand, key=os.path.getmtime)))
        return e['sessionId'], e['prompt'], e['ts']
    if sid:
        ev = os.path.expanduser(f'~/.copilot/session-state/{sid}/events.jsonl')
        if os.path.exists(ev):
            last = None
            for l in open(ev):
                try: e = json.loads(l)
                except ValueError: continue
                if e.get('type') == 'user.message': last = e
            if last:
                return sid, last['data'].get('content', ''), last.get('id') or last.get('timestamp')
    return sid or None, None, None

def pid_alive(pid):
    if not pid: return False
    if os.name == 'nt':
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not h: return False
        code = ctypes.c_ulong(); ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code)); ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259
    try: os.kill(int(pid), 0); return True
    except OSError: return False

def run_status(run):
    try: st = json.load(open(os.path.join(run, 'status.json')))
    except (OSError, ValueError): st = {'state': 'starting'}
    if st['state'] in ('running', 'starting') and not pid_alive(st.get('pid')) and time.time() - st.get('updated', 0) > 5:
        st['state'] = 'interrupted'
    return st

def link(path):
    return f'[{os.path.basename(path)}](file://{path if path.startswith("/") else "/" + path.replace(os.sep, "/")})'

def describe(run, st):
    cr = f"{st.get('credits', 0):.0f} credits, {st.get('minutes', 0):.0f} min so far"
    if st['state'] in ('running', 'starting'):
        step = step_name(st.get('step', 'starting'))
        recent = st.get('recent') or []
        now = ('\n\nLatest actions:\n```text\n' + '\n'.join(recent) + '\n```\n') if recent else ' '
        on = f' on {st["model"]}' if st.get('model') else ''
        return (f'Working: {step}{on} ({cr}; {st.get("actions", 0)} actions in this step).{now}Live view: {link(os.path.join(run, "progress.md"))}. '
                'The next stop opens in an editor tab when it is ready; reply here then.')
    if st['state'] == 'paused':
        return (f"Waiting for your reply at {st['where']} ({cr}). The stop is open in an editor tab "
                f"({link(st['stop_file'])}); reply here with go, your answers, a question, or stop.")
    if st['state'] == 'interrupted':
        return f"The run was interrupted during {st.get('step', 'a step')} ({cr}). Reply `resume` to continue it."
    res = st.get('result', '').splitlines()[0] if st.get('result') else 'ended'
    tail = ' Reply `resume` to continue from there.' if st.get('resumable') else ' Start a new chat for the next story; `report` writes a problem report.'
    return f"Finished: {res}. Final report: {link(st.get('final') or os.path.join(run, 'final.md'))}.{tail}"

def launch(args, run):
    log = open(os.path.join(run, 'ctl.log'), 'a')
    kw = dict(cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    if os.name == 'nt':
        kw['creationflags'] = 0x00000008 | 0x00000200      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw['start_new_session'] = True
    p = subprocess.Popen([sys.executable, os.path.abspath(__file__), *args], **kw)
    json.dump({'state': 'starting', 'pid': p.pid, 'updated': time.time(), 'step': 'starting'}, open(os.path.join(run, 'status.json'), 'w'))
    return p

def passthrough():
    out = []
    if a.fake: out += ['--fake', a.fake]
    if a.no_open: out.append('--no-open')
    if a.log: out += ['--log', a.log]
    if a.no_log: out.append('--no-log')
    return out

def chat():
    sid, msg, ts = read_message()
    if msg is None:
        print('I could not read your message (the story agent\'s hook did not run, so hooks may be turned off here). '
              'Run the story from the terminal instead: python3 <path>/story-ctl.py story.txt'); return
    rc, _ = git('rev-parse', '--is-inside-work-tree')
    if rc:
        print('Open the repository you want to change as the workspace folder, then paste the story again.'); return
    cd = os.path.join(home(), 'chats'); os.makedirs(cd, exist_ok=True)
    cf = os.path.join(cd, re.sub(r'[^\w.-]', '_', sid or 'unknown') + '.json')
    ch = json.load(open(cf)) if os.path.exists(cf) else {}
    if ch.get('last_ts') == ts and ch.get('last_out'):
        print(ch['last_out']); return                       # the same message again: same answer, nothing re-run
    out = chat_reply(ch, msg.strip())
    ch.update(last_ts=ts, last_out=out)
    json.dump(ch, open(cf, 'w'))
    print(out)

def chat_start(ch, msg, word):
    """Start a story from a pasted message: criteria numbered as AC1:, AC2:, ... first if they are not."""
    global S
    pending = ch.pop('pending', None)
    if pending and word in ('go', 'yes', 'y', 'ok', 'start'):
        story, crit, sure = pending, parse_criteria(pending), True
    else:
        story, crit, sure = number_criteria(msg)
    if not crit:
        return ('I could not find acceptance criteria in your message. Paste the whole story from Jira, with its '
                '"Acceptance criteria" heading and the criteria below it (or lines starting "AC1:", "AC2:", ...). '
                'Other messages: `status`, `resume`, `stats`, `report`, `help`.')
    shown = '```text\n' + crit + '\n```'
    if not sure:
        ch['pending'] = story
        return (f'I read these lines as the acceptance criteria:\n\n{shown}\n\nReply `go` to start with them, or paste '
                'the story again with an "Acceptance criteria" line above the criteria.')
    S = run = os.path.join(runs_dir(), new_run_id())
    os.makedirs(run, exist_ok=True)
    sf = os.path.join(run, 'story.txt'); open(sf, 'w').write(story.rstrip('\n') + '\n')
    if story.strip() != msg.strip() and not pending:
        open(os.path.join(run, 'story-pasted.txt'), 'w').write(msg + '\n')
    launch([sf, '--state', run, '--step', '--base', a.base, *passthrough()], run)
    ch['run'] = run
    head = 'Started.' if story.strip() == msg.strip() else f'Started with these acceptance criteria, numbered from your paste:\n\n{shown}\n\nIf they are wrong, reply `stop` and paste the story again.'
    return (head + ' Investigate is reading the code; the plan opens in an editor tab when it is ready '
            f'(usually a few minutes). Reply here then. Live view of what the agents are doing: {link(os.path.join(run, "progress.md"))} '
            '(it opens in a tab and updates by itself). Send `status` any time.')

def chat_reply(ch, msg):
    global S
    word = msg.lower().rstrip('.!')
    if word == 'help': return CHAT_HELP
    if word == 'stats':
        import io, contextlib
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf): stats()
        except SystemExit as e: return str(e)
        return '```\n' + buf.getvalue() + '```'
    run = ch.get('run')
    if word == 'report':
        import io, contextlib
        r = run or resolve_run('last')
        with contextlib.redirect_stdout(io.StringIO()): report(r)
        return f"Problem report: {link(os.path.join(r, 'problem-report.md'))}. It contains your story and replies: replace names before sharing it."
    if not run:
        if word in ('resume', 'continue'):
            try: run = resolve_run('last')
            except SystemExit as e: return str(e)
            ch['run'] = run; msg = word = 'resume'
        else:
            return chat_start(ch, msg, word)
    st = run_status(run)
    if word == 'status': return describe(run, st)
    if st['state'] in ('running', 'starting'):
        if word == 'stop':
            try:
                if os.name == 'nt': os.kill(int(st['pid']), signal.CTRL_BREAK_EVENT)
                else: os.kill(int(st['pid']), signal.SIGINT)
            except OSError: pass
            return 'Stopping. Everything so far is saved; reply `resume` later to continue.'
        return describe(run, st) + ' Your message was not used: send it again when the stop opens.'
    if st['state'] == 'paused':
        rf = os.path.join(run, f'chat-reply-{int(time.time() * 1000)}.txt'); open(rf, 'w').write(msg)
        launch(['--resume', run, '--step', '--reply-file', rf, *passthrough()], run)
        return 'Got it. Continuing; the next stop opens in an editor tab. Send `status` any time.'
    if st['state'] in ('interrupted', 'ended') and word not in ('resume', 'continue', 'go', 'retry') and number_criteria(msg)[1]:
        return chat_start(ch, msg, word)                    # a new story (or the same one pasted again) in this chat
    if st['state'] == 'interrupted' or (st['state'] == 'ended' and st.get('resumable')):
        if word not in ('resume', 'continue', 'go', 'retry'):
            return describe(run, st)
        launch(['--resume', run, '--step', *passthrough()], run)
        return 'Resuming from the last finished step; the next stop opens in an editor tab. Send `status` any time.'
    return describe(run, st)

# ---------- the workflow ----------

def main(argv=None):
    global a, ROOT, S, trace, answers, step, fake_count, cur, usage, t0, prev_seconds, replay, replay_i, pending, meta, reply_text
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
    a = ap.parse_args(argv)
    ROOT = os.getcwd()
    reply_text = open(a.reply_file).read().strip() if a.reply_file else None
    if a.hook_prompt: return hook_prompt()
    if a.hook_guard: return hook_guard()
    fake_count, trace, answers, step, usage = {}, [], [], 0, {}
    cur = {'impl': None, 'rev': None, 'verdicts': [], 'handoff': None}
    t0, prev_seconds, replay, replay_i, pending, meta = time.monotonic(), 0.0, [], 0, None, {}
    if a.chat: return chat()
    if a.stats: return stats()
    if a.report: return report(resolve_run(a.report))
    if a.resume:
        S = resolve_run(a.resume)
        meta = json.load(open(os.path.join(S, 'meta.json')))
        replay, interrupted = prepare_resume(S)
        prev_seconds = sum(e['seconds'] for e in replay if e['type'] == 'session')
        for e in replay:
            if e['type'] == 'start': fake_count[e['agent']] = fake_count.get(e['agent'], 0) + 1
        pending = interrupted
        meta['resumed'] = meta.get('resumed', 0) + 1
        json.dump(meta, open(os.path.join(S, 'meta.json'), 'w'))
        story, base_desc = meta['story_text'], meta['base_desc']
        INV, IMP, REV = meta['agents']; a.explain_agent = meta['explain_agent']; a.cap = meta['cap']
        print(f'[story-ctl] resuming {S}' + (f' (the {interrupted["label"]} step was interrupted)' if interrupted else ''))
    else:
        if not a.story: ap.error('a story file is needed (or --resume, --report, --stats)')
        INV, IMP, REV = a.agents.split(',')
        story = open(a.story).read().strip()
        if not parse_criteria(story):
            story, crit, _ = number_criteria(story)
            if not crit:
                sys.exit('No acceptance criteria found in the story file: put them under an "Acceptance criteria" line, or start each with "AC1:", "AC2:", ...')
            story = story.strip()
            print('[story-ctl] acceptance criteria numbered from the story:\n' + crit, file=sys.stderr)
        rc, out = git('rev-parse', '--verify', '--quiet', a.base + '^{commit}')
        if rc:
            sys.exit(f'--base {a.base} is not a commit in this repository (run story-ctl from the repository root).')
        base_sha = out.decode().strip()
        base_desc = base_sha if a.base == base_sha else f'{base_sha} ({a.base} when the run started)'
        S = a.state or os.path.join(runs_dir(), new_run_id())
        os.makedirs(S, exist_ok=True)
        meta = {'version': VERSION, 'started': time.strftime('%Y-%m-%d %H:%M'), 'story_text': story,
                'story_name': os.path.basename(a.story), 'base_desc': base_desc, 'agents': [INV, IMP, REV],
                'explain_agent': a.explain_agent, 'cap': a.cap, 'resumed': 0}
        json.dump(meta, open(os.path.join(S, 'meta.json'), 'w'))
        set_status('starting', step='starting')
        open_in_editor(os.path.join(S, 'progress.md'))
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
        jwrite({'type': 'session', 'seconds': round(time.monotonic() - t0)})
        write_log('Interrupted')
        try: last = json.load(open(os.path.join(S, 'status.json'))).get('step')
        except (OSError, ValueError): last = None
        set_status('interrupted', step=last or 'a step')
        print(f'\n[story-ctl] interrupted; everything so far is saved. To continue: --resume {S}', file=sys.stderr)
        sys.exit(130)

def workflow(story, criteria, base_desc, INV, IMP, REV):
    # 1. investigate, then stop for the developer
    answers_q = []
    handoff, _ = run_agent(INV, 'Story mode\n\n' + story, 'investigate', True)
    cur['handoff'] = handoff
    while True:
        hq = field(handoff, 'Questions for the analyst')
        r = ask(handoff + '\n\nReply go to implement, answer the questions above (implement and review will use your answers), '
                'start your reply with "Change the plan:" to have investigate plan again, or reply stop.', 1 if not answers else len(answers) + 1, 'the handoff', hq)
        while is_question(r):
            handoff_note = answer_question(r, handoff, 'plan')
            answers_q.append((r, handoff_note))
            r = ask('Answer to your question:\n\n' + handoff_note + '\n\n---\n\n' + handoff + '\n\nReply go to implement, answer the questions above, start your reply with "Change the plan:", or reply stop.', f'h{len(answers_q)}', 'the handoff', hq)
        if re.match(r'\s*change the plan\b', r, re.I):
            handoff, _ = run_agent(INV, f'Story mode\n\n{story}\n\nPrevious handoff:\n{handoff}\n\nChanges from the user (verbatim):\n{r}', 'reinvestigate', True)
            cur['handoff'] = handoff
            continue
        if not is_go(r):
            answers.append(('At the handoff', r))
        break

    # 2. rounds
    rev_task = f'Review the changes on the current branch against base {base_desc}\n\nAcceptance criteria (from the story, verbatim):\n{criteria}\n\nImplement report:\n'
    imp_prompt = 'Round 1 of 3\nImplement the task based on this handoff.\n\n' + handoff + answers_block()
    prev_edit = None
    for n in (1, 2, 3):
        impl, code = run_agent(IMP, imp_prompt, f'implement-r{n}', False)
        cur['impl'] = impl
        why = impl_incomplete(impl, code)
        if why:
            r = ask(f'Implement could not finish round {n} ({why}). Its report:\n\n{impl}\n\nReply "review" to send the work to review anyway, "stop" to stop here, or tell implement what to do next.', f'impl{n}', f'round {n} after implement could not finish ({why})')
            if not re.fullmatch(r'\s*review\.?\s*', r, re.I):
                answers.append((f'During round {n} (to implement)', r))
                impl, code = run_agent(IMP, f'Round {n} of 3\nContinue this round. Your previous report is below; the developer\'s instruction is the last answer.\n\n{impl}{answers_block()}', f'implement-r{n}b', False)
                cur['impl'] = impl
                why2 = impl_incomplete(impl, code)
                if why2:
                    stop_with(f'Stopped — implement could not finish round {n} twice ({why2})')
        inv = field(handoff, 'Behavioural invariants')
        inv_block = '' if (not inv or re.match(r'^(none|n/a)\b', inv, re.I)) else '\n\nBehavioural invariants from the handoff (verbatim):\n' + inv
        rev, _ = run_agent(REV, rev_task + impl + answers_block() + inv_block, f'review-r{n}', True)
        cur['rev'] = rev
        v, bad = verdict_check(rev)
        cur['verdicts'].append(verdict_line(rev).replace('- **Verdict:** ', '') if v else f'invalid ({bad})')
        if v is None:
            stop_with(f'Stopped — the round {n} review report has no valid verdict ({bad}), so no further edits were made; see the last review')
        en = edit_norm(rev)
        asks = v == 'Needs answers' or (v == 'Request changes' and has_questions(rev))
        if v == 'Approve':
            stop_with('Approved')
        if n == 3:
            stop_with('Not approved after 3 rounds' if v == 'Request changes' else v + ' after 3 rounds')
        if not asks and v == 'Request changes' and same_edits(prev_edit, en):
            stop_with(f'Stopped early — the round {n} review asks for the same changes as round {n-1}, so another round is unlikely to help; see the last review')
        prev_edit = en
        if asks:
            rq = field(rev, 'Questions for the author or analyst')
            shown = verdict_line(rev) + '\n- **Questions for the author or analyst:** ' + rq
            if v == 'Request changes':
                shown += '\n- **Edit:** ' + field(rev, 'Edit')
            tail = f'\n\nRound {n} of 3. Reply with your answers and I will continue, or reply stop.'
            r = ask(shown + tail, len(answers) + 2, f'the round {n} review questions', rq)
            while is_question(r):
                note = answer_question(r, rev, 'review')
                r = ask('Answer to your question:\n\n' + note + '\n\n---\n\n' + shown + tail, f'q{n}-{len(answers_q)}', f'the round {n} review questions', rq)
                answers_q.append((r, note))
            if not is_go(r):
                answers.append((f'After round {n} review', r))
            imp_prompt = f'Round {n+1} of 3\nFix the items under Edit in this review, if any, and apply the user\'s answers to its questions.\n\n{rev}{answers_block()}'
        else:
            imp_prompt = f'Round {n+1} of 3\nFix the items under Edit in this review.\n\n{rev}{answers_block()}'

if __name__ == '__main__':
    main()
