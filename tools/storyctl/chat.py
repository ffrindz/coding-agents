"""Chat mode: the story agent in the VS Code agent window (or Copilot CLI).

The story agent runs one command, `story-ctl.py --chat`, for every message and shows its output. Your message
reaches the controller through the agent's userPromptSubmitted hook (--hook-prompt), never through the model;
its preToolUse hook (--hook-guard) denies every other command. --chat starts or continues the run in the
background and returns at once; each stop opens in an editor tab, and your next chat message is the reply."""
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

CHAT_HELP = ('Paste a story with its acceptance criteria to start (straight from Jira is fine: the criteria are numbered '
             'AC1:, AC2:, ... for you). At each stop '
             'the plan or review opens in an editor tab; reply here with `go`, your answers, a question ending in `?`, '
             'or `stop`. Other messages: `status`, `resume` (continue the last stopped run in this repository), '
             '`stats`, `report`.')

CHAT_CMD = re.compile(r'''^\s*(?:cd\s+\S+\s*(?:&&|;)\s*)?(?:python3?|py)(?:\s+-I)?\s+(?:"[^"]*story-ctl\.py"|'[^']*story-ctl\.py'|[^"'\s;&|<>`$()]*story-ctl\.py|\$HOME/[^"'\s;&|<>`$()]*story-ctl\.py|"\$env:[^"]*story-ctl\.py")\s+--chat\s*(?:2>&1)?\s*$''')

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

SCRIPT_ARG_RE = re.compile(r'''(?:python3?|py)(?:\s+-I)?\s+("[^"]*"|'[^']*'|\S+)\s+--chat''')

def trusted_script(cmd):
    """True when the --chat command runs the installed controller (~/.story-ctl/story-ctl.py, or the one in
    $STORY_CTL_HOME), not another file of the same name."""
    m = SCRIPT_ARG_RE.search(cmd)
    if not m: return False
    tok = m.group(1); q = tok[0] if tok[:1] in ('"', "'") else ''
    if os.name != 'nt' and '\\' in tok: return False     # a backslash is part of the file name there, not a separator
    path = tok[1:-1] if q else tok
    # what a shell would expand: ~ only unquoted; $HOME, ${HOME} and $env:USERPROFILE unquoted or in double quotes
    pres = [] if q == "'" else ['$env:USERPROFILE', '%USERPROFILE%', '${HOME}', '$HOME'] + ([] if q else ['~'])
    for pre in pres:
        if path.startswith(pre) and (len(path) == len(pre) or path[len(pre)] in '/\\'):
            base = (os.environ.get('USERPROFILE') if 'USERPROFILE' in pre else None) or os.path.expanduser('~')
            path = base + path[len(pre):]; break
    def norm(x):
        return os.path.normcase(os.path.normpath(x))
    homes = {os.path.expanduser('~'), os.environ.get('HOME') or '', os.environ.get('USERPROFILE') or ''} - {''}
    ok = {norm(os.path.join(h, '.story-ctl', 'story-ctl.py')) for h in homes} | {norm(os.path.join(home(), 'story-ctl.py'))}
    return norm(path) in ok

def hook_guard():
    """preToolUse hook of the story agent: allow only the story-ctl --chat command."""
    d = hook_input()
    name = str(d.get('toolName') or d.get('tool_name') or '')
    args = d.get('toolArgs') if 'toolArgs' in d else d.get('tool_input')
    if isinstance(args, str):
        try: args = json.loads(args)
        except ValueError: args = {'command': args}
    cmd = (args or {}).get('command') if isinstance(args, dict) else None
    if name in READ_ONLY_TOOLS or (isinstance(cmd, str) and CHAT_CMD.match(cmd) and trusted_script(cmd)):
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
        here = os.path.realpath(G.ROOT)
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
    pid = st.get('pid')
    if not pid:
        try: pid = int(open(os.path.join(run, 'worker.pid')).read().strip())
        except (OSError, ValueError): pid = None
    st['pid'] = pid
    if st['state'] in ('running', 'starting') and not pid_alive(pid) and time.time() - st.get('updated', 0) > (5 if pid else 30):
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
    kw = dict(cwd=G.ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    if os.name == 'nt':
        kw['creationflags'] = 0x00000008 | 0x00000200      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw['start_new_session'] = True
    tmp = os.path.join(run, 'status.json.tmp')                # written before the worker starts, so it never overwrites the worker's
    json.dump({'state': 'starting', 'pid': None, 'updated': time.time(), 'step': 'starting'}, open(tmp, 'w'))
    os.replace(tmp, os.path.join(run, 'status.json'))
    p = subprocess.Popen([sys.executable, ENTRY, *args], **kw)
    open(os.path.join(run, 'worker.pid'), 'w').write(str(p.pid))
    return p

def passthrough():
    out = []
    if G.a.fake: out += ['--fake', G.a.fake]
    if G.a.no_open: out.append('--no-open')
    if G.a.log: out += ['--log', G.a.log]
    if G.a.no_log: out.append('--no-log')
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
    out = chat_reply(ch, msg)                   # exactly as typed; only command words are compared stripped
    ch.update(last_ts=ts, last_out=out)
    json.dump(ch, open(cf, 'w'))
    print(out)

def active_run(exclude=None):
    """(run folder, status) of a run in this working tree that is working or waiting for a reply, else (None, None)."""
    d = runs_dir()
    if not os.path.isdir(d): return None, None
    for r in sorted(os.listdir(d), reverse=True):
        run = os.path.join(d, r)
        if not os.path.isdir(run) or (exclude and os.path.realpath(run) == os.path.realpath(exclude)): continue
        st = run_status(run)
        if st['state'] in ('running', 'starting', 'paused'): return run, st
    return None, None

class start_lock:
    """Held while a run is checked for and started, so two chats pasting at the same moment cannot both start."""
    def __enter__(self):
        os.makedirs(runs_dir(), exist_ok=True); self.p = os.path.join(runs_dir(), 'start.lock')
        for _ in range(100):
            try: self.fd = os.open(self.p, os.O_CREAT | os.O_EXCL | os.O_WRONLY); return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.p) > 30: os.remove(self.p); continue   # left by a crash
                except OSError: pass
                time.sleep(0.1)
        raise SystemExit('Another story is being started in this repository; try again in a few seconds.')
    def __exit__(self, *exc):
        os.close(self.fd)
        try: os.remove(self.p)
        except OSError: pass

def abandon(run, st):
    """End a run that is waiting for a reply so a new story can start; it stays resumable, nothing is reverted."""
    with open(os.path.join(run, 'journal.jsonl'), 'a') as f:
        f.write(json.dumps({'type': 'end', 'result': 'Set aside for a new story in the same repository', 'resumable': True, 'by_reply': False}) + '\n')
    st.update(state='ended', result='Set aside for a new story in the same repository', resumable=True, updated=time.time())
    json.dump(st, open(os.path.join(run, 'status.json'), 'w'))

def chat_start(ch, msg, word):
    """Start a story from a pasted message: criteria numbered as AC1:, AC2:, ... first if they are not."""
    try:
        with start_lock(): return _chat_start(ch, msg, word)
    except SystemExit as e: return str(e)

def _chat_start(ch, msg, word):
    held = ch.pop('held', None)
    if held and word in ('set aside', 'set it aside', 'abandon'):
        other, st = active_run()
        if other and st['state'] != 'paused':
            ch['held'] = held
            return f'The other story is still working ({describe(other, st)}). Send `stop` in its chat first, or wait for its next stop.'
        if other: abandon(other, st)
        msg, word = held, ''
    pending = ch.pop('pending', None)
    if pending and word in ('go', 'yes', 'y', 'ok', 'start'):
        story, crit, sure = pending, parse_criteria(pending), True
    else:
        story, crit, sure = number_criteria(msg)
    if not crit:
        return ('I could not find acceptance criteria in your message. Paste the whole story from Jira, with its '
                '"Acceptance criteria" heading and the criteria below it (or lines starting "AC1:", "AC2:", ...). '
                'Other messages: `status`, `resume`, `stats`, `report`, `help`.')
    other, ost = active_run()
    if other:
        ch['held'] = msg
        what = 'is still working' if ost['state'] != 'paused' else f"is waiting for a reply at {ost.get('where', 'a stop')}"
        return (f'Another story in this repository {what} (run {os.path.basename(other)}). Two stories in one working tree '
                'would mix their changes and reviews. Finish or stop that one in its chat, then paste this story again; or reply '
                '`set aside` to end the waiting one (it stays resumable, nothing is reverted) and start this one. '
                'For two stories at once, use a second worktree (git worktree add).')
    shown = '```text\n' + crit + '\n```'
    if not sure:
        ch['pending'] = story
        return (f'I read these lines as the acceptance criteria:\n\n{shown}\n\nReply `go` to start with them, or paste '
                'the story again with an "Acceptance criteria" line above the criteria.')
    G.S = run = os.path.join(runs_dir(), new_run_id())
    os.makedirs(run, exist_ok=True)
    sf = os.path.join(run, 'story.txt'); open(sf, 'w').write(story.rstrip('\n') + '\n')
    if story.strip() != msg.strip() and not pending:
        open(os.path.join(run, 'story-pasted.txt'), 'w').write(msg + '\n')
    launch([sf, '--state', run, '--step', '--base', G.a.base, *passthrough()], run)
    ch['run'] = run
    head = 'Started.' if story.strip() == msg.strip() else f'Started with these acceptance criteria, numbered from your paste:\n\n{shown}\n\nIf they are wrong, reply `stop` and paste the story again.'
    return (head + ' Investigate is reading the code; the plan opens in an editor tab when it is ready '
            f'(usually a few minutes). Reply here then. Live view of what the agents are doing: {link(os.path.join(run, "progress.md"))} '
            '(it opens in a tab and updates by itself). Send `status` any time.')

def chat_reply(ch, msg):
    word = msg.strip().lower().rstrip('.!')
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
            other, ost = active_run(exclude=run)
            if other: return f'Another story in this repository is {ost["state"]} (run {os.path.basename(other)}); finish or stop it first.'
            ch['run'] = run; msg = word = 'resume'
        else:
            return chat_start(ch, msg, word)
    st = run_status(run)
    if word == 'status': return describe(run, st)
    if st['state'] in ('running', 'starting'):
        if word == 'stop':
            if not st.get('pid'): return 'It is just starting; send `stop` again in a few seconds.'
            try:
                if os.name == 'nt': os.kill(int(st['pid']), signal.CTRL_BREAK_EVENT)
                else: os.kill(int(st['pid']), signal.SIGINT)
            except (OSError, ValueError, TypeError): pass
            return 'Stopping. Everything so far is saved; reply `resume` later to continue.'
        return describe(run, st) + ' Your message was not used: send it again when the stop opens.'
    if st['state'] == 'paused':
        rf = os.path.join(run, f'chat-reply-{int(time.time() * 1000)}.txt'); open(rf, 'w', encoding='utf-8', newline='').write(msg)
        launch(['--resume', run, '--step', '--reply-file', rf, *passthrough()], run)
        return 'Got it. Continuing; the next stop opens in an editor tab. Send `status` any time.'
    if st['state'] in ('interrupted', 'ended') and word not in ('resume', 'continue', 'go', 'retry') and number_criteria(msg)[1]:
        return chat_start(ch, msg, word)                    # a new story (or the same one pasted again) in this chat
    if st['state'] == 'interrupted' or (st['state'] == 'ended' and st.get('resumable')):
        if word not in ('resume', 'continue', 'go', 'retry'):
            return describe(run, st)
        other, ost = active_run(exclude=run)
        if other: return f'Another story in this repository is {ost["state"]} (run {os.path.basename(other)}); finish or stop it first.'
        launch(['--resume', run, '--step', *passthrough()], run)
        return 'Resuming from the last finished step; the next stop opens in an editor tab. Send `status` any time.'
    return describe(run, st)

__all__ = [n for n in dir() if not n.startswith('__')]
