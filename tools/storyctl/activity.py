"""What a running agent is doing, read from its Copilot session log."""
import argparse, difflib, hashlib, json, os, re, shlex, shutil, signal, subprocess, sys, time, uuid
from .state import *
from .criteria import *
from .reports import *
from .repo import *
from .journal import *

ACT_SKIP = {'report_intent', 'think', 'update_todo', 'read_bash', 'list_bash', 'read_powershell', 'list_powershell'}

def _rel(p):
    p = str(p or '')
    root = G.ROOT or ''
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
        self.rev = 0                                                # bumped by every change the page shows
    def read(self):
        try:
            with open(self.path, encoding='utf-8', errors='replace') as f:
                f.seek(self.pos); data = f.read(); self.pos = f.tell()
        except OSError:
            return self.acts
        lines = (self.buf + data).split('\n'); self.buf = lines.pop()
        for l in lines:
            try: self._event(json.loads(l))
            except Exception: continue          # a line in a shape we do not know: skip it
        return self.acts
    def _event(self, e):
        if not isinstance(e, dict): return
        t, d = e.get('type'), e.get('data') if isinstance(e.get('data'), dict) else {}
        self.rev += 1
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

__all__ = [n for n in dir() if not n.startswith('__')]
