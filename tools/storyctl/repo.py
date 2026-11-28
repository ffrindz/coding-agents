"""The git repository: commands, working-tree snapshots, the run folder."""
import argparse, difflib, hashlib, json, os, re, shlex, shutil, signal, subprocess, sys, time, uuid
from .state import *
from .criteria import *
from .reports import *

def git(*args):
    p = subprocess.run(['git', *args], capture_output=True, text=False, cwd=G.ROOT)
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
        p = os.path.join(G.ROOT, f)
        if os.path.realpath(p).startswith(os.path.realpath(G.S) + os.sep): continue
        try: h.update(f.encode() + open(p, 'rb').read())
        except OSError: h.update(f.encode())
    return h.hexdigest()

def changed_files():
    _, out = git('status', '--porcelain=v1', '-uall')
    return '\n'.join(out.decode(errors='replace').splitlines()[:30]) or '(no files listed by git status; the staged or unstaged diff changed)'

def repo_state():
    _, h = git('rev-parse', '--verify', '--quiet', 'HEAD'); _, b = git('symbolic-ref', '--short', '-q', 'HEAD')
    return {'head': h.decode().strip(), 'branch': b.decode().strip() or '(detached)'}

def new_run_id():
    return time.strftime('%Y%m%d-%H%M%S') + '-' + os.urandom(2).hex()

def runs_dir():
    _, gd = git('rev-parse', '--absolute-git-dir')
    return os.path.join(gd.decode().strip() or os.path.join(G.ROOT, '.git'), 'story-ctl')

__all__ = [n for n in dir() if not n.startswith('__')]
