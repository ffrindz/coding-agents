"""Running one agent, stops and replies, and ending a run."""
import argparse, difflib, hashlib, json, os, re, shlex, shutil, signal, subprocess, sys, time, uuid
from .state import *
from .criteria import *
from .reports import *
from .repo import *
from .journal import *
from .activity import *
from .progress import *
from .notify import *

def run_agent(agent, prompt, label, read_only):
    """One fresh copilot session; returns (final message, exit code). Retries once when nothing came back,
    unless the working tree changed during the failed attempt."""
    saved = replay_next('task')
    if saved is not None:
        if saved['agent'] != agent or saved['label'] != label or saved['phash'] != phash(prompt):
            sys.exit(f'Cannot resume: the saved {saved["label"]} step does not match this run ({label}). Start a new run.')
        G.step += saved['attempts']; G.usage[agent] = G.usage.get(agent, 0) + saved['credits']
        G.trace.append(['task', agent, prompt, saved['result'], [], saved['code']])
        print(f'[story-ctl] resumed: {label} (saved result)')
        return saved['result'], saved['code']
    if G.pending and G.pending['label'] == label and not G.pending['read_only']:
        r = ask(f'{agent} ({label}) was interrupted while it was running, so it may have left partial edits. Files changed '
                f'now:\n{changed_files()}\n\nReply "retry" to run that step again over the current files, or "stop".',
                'resume', 'the resume of an interrupted step', record=False)
        if not re.fullmatch(r'\s*(retry|go|resume|continue)\.?\s*', r, re.I):
            stop_with(f'Stopped at the resume of an interrupted step (reply was not "retry")', resumable=True)
    G.pending = None
    deny = READ_ONLY_DENY if read_only else WRITE_DENY
    spent, t_start = 0.0, time.monotonic()
    for attempt in (1, 2):
        G.LIVE = {'label': label, 'agent': agent, 'since': time.time(), 'acts': []}
        set_status('running', step=label, agent=agent)
        G.step += 1
        pf = save(f'{G.step:02d}-{label}.prompt.md', prompt)
        before = snapshot()
        final, tools, code, cr = '', [], 0, 0.0
        jwrite({'type': 'start', 'agent': agent, 'label': label, 'read_only': read_only})
        if G.a.fake:
            k = G.fake_count[agent] = G.fake_count.get(agent, 0) + 1
            base = os.path.join(os.path.abspath(G.a.fake), f'{agent}-{k}')
            if os.path.exists(base + '.events'):
                G.LIVE['act'] = Activity(base + '.events'); G.LIVE['acts'] = G.LIVE['act'].read(); set_status('running', step=label, agent=agent)
            if os.path.exists(base + '.sh'):
                subprocess.run(['sh', base + '.sh'], cwd=G.ROOT)
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
            cmd += ['--max-ai-credits', str(G.a.cap), '--disable-client-telemetry']
            ev = os.path.expanduser(f'~/.copilot/session-state/{sid}/events.jsonl')
            act = Activity(ev); G.LIVE['acts'] = act.acts; G.LIVE['act'] = act
            lf = os.path.join(G.S, f'{G.step:02d}-{label}.stdout.txt')
            with open(lf, 'w') as out_f:
                p = subprocess.Popen(cmd, stdout=out_f, stderr=subprocess.STDOUT, cwd=G.ROOT)
                try:
                    seen, tick, warned = -1, 0, False
                    while True:
                        try: p.wait(timeout=2); break
                        except subprocess.TimeoutExpired: pass
                        tick += 1
                        try:                            # the live view is best effort: it never stops the agent
                            act.read()
                            if act.rev != seen or tick % 5 == 0:
                                seen = act.rev; set_status('running', step=label, agent=agent)
                        except (Exception, SystemExit) as e:
                            if not warned: print(f'[story-ctl] live view not updated: {e!r}', file=sys.stderr); warned = True
                except BaseException:
                    p.kill(); p.wait(); raise
            code = p.returncode
            log = open(lf, encoding='utf-8', errors='replace').read()
            cr = credits_in(log)
            try: act.read()
            except Exception: pass
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
        spent += cr; G.usage[agent] = G.usage.get(agent, 0) + cr
        after = snapshot()
        touched = before is not None and after != before
        G.trace.append(['task', agent, prompt, final, tools, code])
        actf, mdesc = None, model_desc(G.LIVE.get('act')) if G.LIVE else ''
        if G.LIVE and G.LIVE['acts']:
            actf = f'{G.step:02d}-{label}.activity.md'
            save(actf, f'# What {agent} did ({step_name(label)})\n\n' + (f'Model: {mdesc}\n\n' if mdesc else '') + '```text\n' + '\n'.join(activity_lines(G.LIVE['acts'], short=False)) + '\n```\n')
        G.LIVE = None
        if final and read_only and code:        # never route on (or approve from) a process that failed
            save(f'{G.step:02d}-{label}.failed.md', final)
            print(f'\n[story-ctl] {agent} exited with code {code}, so its report is not used (attempt {attempt}); '
                  f'see {G.step:02d}-{label}.failed.md', file=sys.stderr)
            final = ''
        if final:
            save(f'{G.step:02d}-{label}.result.md', final)
            jwrite({'type': 'task', 'agent': agent, 'label': label, 'phash': phash(prompt), 'result': final, 'code': code,
                    'credits': spent, 'attempts': attempt, 'seconds': round(time.monotonic() - t_start),
                    'file': f'{G.step:02d}-{label}.result.md', 'activity': actf, 'model': mdesc})
        if read_only and touched:
            stop_with(f'Stopped — the {agent} step changed the working tree, but it must not edit. Nothing was '
                      f'reverted; check these files:\n{changed_files()}')
        if final:
            if code: print(f'[story-ctl] {agent} exited with code {code}', file=sys.stderr)
            return final, code
        if touched:
            stop_with(f'Stopped — the {agent} step returned no report after changing files, so it was not retried '
                      f'over the partial edits. Nothing was reverted; its output is in {G.S}. Changed files:\n{changed_files()}')
        print(f'\n[story-ctl] {agent} returned no usable report (attempt {attempt}, exit code {code}); see {pf}', file=sys.stderr)
    stop_with(f'Stopped — the {agent} step failed twice; its output is in {G.S}. When the service is back, resume with '
              f'--resume {G.S}', resumable=True)

def ask(text, n, where, asked='', record=True):
    """Show a stop and get the developer's reply. An empty reply, end of input, "stop" or a failed reply
    command ends the run here (it can be resumed at this stop later). In chat mode (--step) the run pauses
    here instead, and the next chat message resumes it with --reply-file."""
    if record:
        saved = replay_next('reply')
        if saved is not None:
            G.trace.append(['reply', text]); G.trace.append(['user', saved['text']])
            print(f'[story-ctl] resumed: your reply at {where} (saved)')
            return saved['text']
    text = text + f'\n\n(so far: {total_credits():.0f} credits, {elapsed() / 60:.0f} min)'
    shown = re.sub(r'\]\((?!https?:|/|#|mailto:)([^)\s]+)\)', lambda m: f']({G.ROOT}/{m.group(1)})', text)
    stop_file = save(f'stop-{n}.md', shown)
    failed = ''
    moved = repo_moved(where) if G.reply_text is not None else ''
    if moved:                                   # do not act on a reply given for a repository that has changed since
        G.reply_text = None
        text = (f'**Your reply was not used:** {moved} since this stop opened. Check that this is what you want, then reply '
                'again (go, your answers, or stop) to continue on the repository as it is now.\n\n---\n\n' + text)
        shown = re.sub(r'\]\((?!https?:|/|#|mailto:)([^)\s]+)\)', lambda m: f']({G.ROOT}/{m.group(1)})', text)
        stop_file = save(f'stop-{n}.md', shown)
    if G.reply_text is not None:                  # chat mode: the developer's message, captured by the hook
        r, G.reply_text = G.reply_text, None
        print(f'[story-ctl] your reply at {where} (from the chat)')
    elif G.a.step:                                # chat mode: pause; the next chat message resumes the run
        jwrite({'type': 'session', 'seconds': round(time.monotonic() - G.t0)})
        jwrite({'type': 'pause', 'where': where, **repo_state()})
        set_status('paused', where=where, stop_file=stop_file)
        open_in_editor(stop_file); notify(stop_note(where))
        print(f'[story-ctl] paused at {where}: {stop_file}')
        sys.exit(10)
    else:
        print('\n' + '=' * 78 + '\n' + text + '\n' + '=' * 78)
        print(f'[story-ctl] this stop is also in {stop_file} (Markdown preview: Cmd+Shift+V; links open the code)')
        if G.a.reply_cmd:
            p = subprocess.run(G.a.reply_cmd.format(stop_file=stop_file, n=n), shell=True, capture_output=True, text=True)
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
    G.trace.append(['reply', text]); G.trace.append(['user', r]); save(f'reply-{n}.md', r)
    if record:
        jwrite({'type': 'reply', 'where': where, 'asked': asked, 'text': r, 'failed': failed})
    if record and (failed or is_stop(r)):
        why = failed or ('no reply' if not r.strip() else 'you')
        stop_with(f'Stopped at {where} ({why}); nothing further was changed. To continue from this stop later: '
                  + ('reply resume in the chat' if G.a.step else f'--resume {G.S}'), resumable=True, by_reply=True)
    return r

def repo_moved(where):
    """What changed in the repository since the run paused at this stop: a branch switch or a new commit."""
    pauses = [e for e in load_journal(G.S) if e['type'] == 'pause' and e['where'] == where]
    if not pauses or 'head' not in pauses[-1]: return ''
    was, now = pauses[-1], repo_state()
    if was['branch'] != now['branch']: return f"the branch changed from {was['branch']} to {now['branch']}"
    if was['head'] != now['head']: return f"{now['branch']} moved from commit {was['head'][:9]} to {now['head'][:9]}"
    return ''

def answers_block():
    if not G.answers: return ''
    return ('\n\nAnswers from the user (verbatim; a later answer replaces an earlier one on the same point):\n'
            + '\n'.join(f'{lab}:\n{txt}' for lab, txt in G.answers))

def answer_question(q, context, what):
    ans, _ = run_agent(G.a.explain_agent, f'A developer reviewing this {what} asks (verbatim): {q}\n\nAnswer the question from the code and the {what} below. Do not change anything, and do not plan new work.\n\n{what.capitalize()}:\n{context}', 'explain-question', True)
    return ans

def stop_with(result, resumable=False, by_reply=False):
    impl, rev, rounds = G.cur['impl'], G.cur['rev'], G.cur['verdicts']
    by = ', '.join(f'{k} {v:.0f}' for k, v in G.usage.items())
    rep = [f'# Story (story-ctl v{VERSION})', f'- **Result:** {result}',
           f'- **Rounds:** {len(rounds)} implement + review rounds; ' + '; '.join(f'round {i+1}: {v}' for i, v in enumerate(rounds)),
           f'- **Credits and time:** {total_credits():.0f} credits ({by or "none"}); {elapsed() / 60:.0f} min']
    if rev:
        rep += ['- **Criteria:**\n' + field(rev, 'Criteria'),
                '- **Open items:**', '  - Findings: ' + (field(rev, 'Findings') or 'none'),
                '  - Questions: ' + (field(rev, 'Questions for the author or analyst') or 'none'),
                '  - Not verified / risks: ' + (field(rev, 'Not verified / risks') or 'none')]
    hq = field(G.cur['handoff'], 'Questions for the analyst') if G.cur['handoff'] else ''
    if hq and not re.match(r'^(none|n/a)\b', hq, re.I):
        rep += ['  - Handoff questions (check that each was answered, or that its assumption is what you want):\n' + hq]
    rep += ['  - Your answers so far:' + (answers_block() or ' none')]
    if impl: rep += ['\n## Last implement report\n', impl]
    if rev: rep += ['\n## Last review\n', rev]
    final = '\n'.join(rep)
    save('final.md', final); G.trace.append(['reply', final])
    json.dump(G.trace, open(os.path.join(G.S, 'trace.json'), 'w'))
    jwrite({'type': 'end', 'result': result, 'resumable': resumable, 'by_reply': by_reply})
    jwrite({'type': 'session', 'seconds': round(time.monotonic() - G.t0)})
    write_log(result)
    set_status('ended', result=result, resumable=resumable, final=os.path.join(G.S, 'final.md'))
    if not by_reply:                            # you stopped it yourself: no need to tell you
        open_in_editor(os.path.join(G.S, 'final.md'))
        notify(_short(result.splitlines()[0], 120).rstrip('.') + '. The final report is open in an editor tab.')
    print('\n' + final + f'\n\n[story-ctl] everything is saved in {G.S}; a problem report for this run: --report {G.S}')
    sys.exit(0)

__all__ = [n for n in dir() if not n.startswith('__')]
