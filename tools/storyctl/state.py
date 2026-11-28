"""Run state shared by every part (G), versions and tool permissions."""
import argparse, difflib, hashlib, json, os, re, shlex, shutil, signal, subprocess, sys, time, uuid

class State:
    """Everything a run shares between its parts: the parsed options (a), the repository root, the run folder (S),
    the trace, the developer's answers, the step counter, replay position, usage, and the agent step running now."""
    def __init__(self):
        self.a = None; self.ROOT = None; self.S = None; self.trace = []; self.answers = []; self.step = 0
        self.fake_count = {}; self.cur = {}; self.usage = {}; self.t0 = 0.0; self.prev_seconds = 0
        self.replay = []; self.replay_i = 0; self.pending = None; self.meta = {}; self.reply_text = None
        self.LIVE = None                        # the agent step running now: {'label', 'agent', 'since', 'acts', 'act'}

G = State()

VERSION = '1.9'
ENTRY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'story-ctl.py')   # the script the chat starts runs with

READ_ONLY_DENY = ['write', 'shell(git push)', 'shell(git commit)', 'shell(git reset)', 'shell(git checkout)',
                  'shell(git stash)', 'shell(git clean)', 'shell(git restore)']

WRITE_DENY = ['shell(git push)']

__all__ = [n for n in dir() if not n.startswith('__')]
