"""Reading the agents' reports: verdicts, fields, replies (pure functions)."""
import argparse, difflib, hashlib, json, os, re, shlex, shutil, signal, subprocess, sys, time, uuid
from .state import *
from .criteria import *

def strip_fences(r):
    return re.sub(r'^(```|~~~).*?^\1[^\n]*$', '', r, flags=re.M | re.S)

def verdict_check(r, n_criteria=None):
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
    c = re.search(r'(\d+)\s+of\s+(\d+)\s+criteri', lines[0])
    if v == 'Approve' and c and int(c.group(1)) < int(c.group(2)):
        return None, f'Approve with only {c.group(1)} of {c.group(2)} criteria met'
    if v == 'Approve' and c and n_criteria and int(c.group(2)) < n_criteria:
        return None, f'Approve counting {c.group(2)} criteria, but the story has {n_criteria}'
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

__all__ = [n for n in dir() if not n.startswith('__')]
