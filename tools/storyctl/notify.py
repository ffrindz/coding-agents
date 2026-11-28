"""Editor tabs and desktop notifications."""
import argparse, difflib, hashlib, json, os, re, shlex, shutil, signal, subprocess, sys, time, uuid
from .state import *
from .criteria import *
from .reports import *
from .repo import *
from .journal import *
from .activity import *
from .progress import *

WIN_TOAST = (r"$ErrorActionPreference='Stop';"
             r"[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]|Out-Null;"
             r"$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
             r"$t=$x.GetElementsByTagName('text');$t.Item(0).AppendChild($x.CreateTextNode($env:STORY_CTL_N_TITLE))|Out-Null;"
             r"$t.Item(1).AppendChild($x.CreateTextNode($env:STORY_CTL_N_TEXT))|Out-Null;"
             r"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe').Show([Windows.UI.Notifications.ToastNotification]::new($x))")

def story_title():
    try: story = json.load(open(os.path.join(G.S, 'meta.json'))).get('story_text', '')
    except (OSError, ValueError): story = ''
    return _short(next((l.strip('#* ').strip() for l in story.splitlines() if l.strip()), 'story'), 60)

def notify(text):
    """A desktop notification (macOS: osascript; Windows: a PowerShell toast; Linux: notify-send). Never fails the
    run. STORY_CTL_NOTIFY=0 turns it off; STORY_CTL_NOTIFY_CMD runs a command of your own with the title and text."""
    if os.environ.get('STORY_CTL_NOTIFY', '1').strip().lower() in ('0', 'off', 'no', 'false'): return
    title = 'Story: ' + story_title()
    env = dict(os.environ, STORY_CTL_N_TITLE=title, STORY_CTL_N_TEXT=text)
    if os.environ.get('STORY_CTL_NOTIFY', '').strip().lower() == 'private': title = 'Story'      # no story title on a lock screen
    env['STORY_CTL_N_TITLE'] = title
    own = os.environ.get('STORY_CTL_NOTIFY_CMD')
    if own: cmd = [*shlex.split(own, posix=os.name != 'nt'), title, text]
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
    if G.a.no_open: return
    ed = os.environ.get('STORY_CTL_EDITOR')
    ed = (shlex.split(ed, posix=os.name != 'nt') if not os.path.exists(ed) else [ed]) if ed else ([shutil.which('code')] if shutil.which('code') else None)
    if ed:
        try: subprocess.run([*ed, '-r', path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        except (OSError, subprocess.SubprocessError): pass

__all__ = [n for n in dir() if not n.startswith('__')]
