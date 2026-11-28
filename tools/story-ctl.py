#!/usr/bin/env python3
"""story-ctl: run a whole story through investigate, implement and review, with you in the loop.

A small, deterministic workflow controller for the Copilot CLI. The three agents do the work; this script does
the bookkeeping that a model does unreliably: it passes every handoff and report on byte for byte, keeps all
your answers and gives them to every later task, reads the review verdict, counts rounds (at most 3), and stops
for you after the handoff and whenever a review has questions.

usage (from the root of the repository you are changing, for example in VS Code's terminal):
    python3 <path>/story-ctl.py story.txt [--base main]

story.txt holds the story with its acceptance criteria as lines starting "AC1:", "AC2:", ... (a criterion may
continue on indented or bulleted lines below it).
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
Test options: --fake <folder> plays back canned agent replies (<agent>-<k>.md; optional <agent>-<k>.sh runs
first in the repository, <agent>-<k>.exit gives the exit code); --reply-cmd answers stops from a command.
Tests: python3 tools/test_story_ctl.py (no Copilot, no credits).
"""
import argparse, difflib, hashlib, json, os, re, shutil, subprocess, sys, time

VERSION = '1.1'
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

# ---------- running agents and stops ----------

def save(name, text):
    open(os.path.join(S, name), 'w').write(text)
    return os.path.join(S, name)

def run_agent(agent, prompt, label, read_only):
    """One fresh copilot session; returns (final message, exit code). Retries once when nothing came back,
    unless the working tree changed during the failed attempt."""
    global step
    deny = READ_ONLY_DENY if read_only else WRITE_DENY
    for attempt in (1, 2):
        step += 1
        pf = save(f'{step:02d}-{label}.prompt.md', prompt)
        before = snapshot()
        final, tools, code = '', [], 0
        if a.fake:
            k = fake_count[agent] = fake_count.get(agent, 0) + 1
            base = os.path.join(os.path.abspath(a.fake), f'{agent}-{k}')
            if os.path.exists(base + '.sh'):
                subprocess.run(['sh', base + '.sh'], cwd=ROOT)
            if os.path.exists(base + '.exit'):
                code = int(open(base + '.exit').read().strip() or 0)
            final = open(base + '.md').read().strip() if os.path.exists(base + '.md') else ''
            if not final: print(f'[story-ctl] fake: no {base}.md', file=sys.stderr)
        else:
            cmd = ['copilot', '--agent', agent, '-p', prompt, '--allow-all-tools']
            for d in deny: cmd += ['--deny-tool', d]
            cmd += ['--max-ai-credits', str(a.cap), '--disable-client-telemetry']
            out = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
            code = out.returncode
            log = out.stdout + out.stderr; save(f'{step:02d}-{label}.stdout.txt', log)
            sid = (re.findall(r'resume=([0-9a-f-]+)', log) or [None])[-1]
            ev = os.path.expanduser(f'~/.copilot/session-state/{sid}/events.jsonl') if sid else None
            if ev and os.path.exists(ev):
                for l in open(ev):
                    try: e = json.loads(l)
                    except ValueError: continue
                    d = e.get('data') or {}
                    if e.get('type') == 'assistant.message' and d.get('content'):
                        final = d['content']
                    if e.get('type') == 'tool.execution_start':
                        tools.append(d.get('toolName'))
            final = re.sub(r'\n*\[cost:[^\]]*\]\s*$', '', final).strip()
        after = snapshot()
        touched = before is not None and after != before
        trace.append(['task', agent, prompt, final, tools, code])
        if final: save(f'{step:02d}-{label}.result.md', final)
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
    stop_with(f'Stopped — the {agent} step failed twice; its output is in {S}.')

def ask(text, n, where):
    """Show a stop and get the developer's reply. An empty reply, end of input, "stop" or a failed reply
    command ends the run here."""
    shown = re.sub(r'\]\((?!https?:|/|#|mailto:)([^)\s]+)\)', lambda m: f']({ROOT}/{m.group(1)})', text)
    stop_file = save(f'stop-{n}.md', shown)
    print('\n' + '=' * 78 + '\n' + text + '\n' + '=' * 78)
    print(f'[story-ctl] this stop is also in {stop_file} (Markdown preview: Cmd+Shift+V; links open the code)')
    if not a.reply_cmd and not a.no_open and shutil.which('code'):
        subprocess.run(['code', '-r', stop_file])
    failed = ''
    if a.reply_cmd:
        p = subprocess.run(a.reply_cmd.format(stop_file=stop_file, n=n), shell=True, capture_output=True, text=True)
        r = p.stdout.strip()
        if p.returncode: failed = f'the reply command exited with code {p.returncode}'
        print(f'[reply] {r}')
    else:
        print('Your reply (empty line to finish; "stop" to end the run):')
        lines = []
        for l in sys.stdin:
            if not l.strip() and lines: break
            lines.append(l.rstrip('\n'))
        r = '\n'.join(lines).strip()
    trace.append(['reply', text]); trace.append(['user', r]); save(f'reply-{n}.md', r)
    if failed or is_stop(r):
        why = failed or ('no reply' if not r.strip() else 'you')
        stop_with(f'Stopped at {where} ({why}); nothing further was changed')
    return r

def answers_block():
    if not answers: return ''
    return ('\n\nAnswers from the user (verbatim; a later answer replaces an earlier one on the same point):\n'
            + '\n'.join(f'{lab}:\n{txt}' for lab, txt in answers))

def answer_question(q, context, what):
    ans, _ = run_agent(a.explain_agent, f'A developer reviewing this {what} asks (verbatim): {q}\n\nAnswer the question from the code and the {what} below. Do not change anything, and do not plan new work.\n\n{what.capitalize()}:\n{context}', 'explain-question', True)
    return ans

def stop_with(result):
    impl, rev, rounds = cur['impl'], cur['rev'], cur['verdicts']
    rep = [f'# Story (story-ctl v{VERSION})', f'- **Result:** {result}',
           f'- **Rounds:** {len(rounds)} implement + review rounds; ' + '; '.join(f'round {i+1}: {v}' for i, v in enumerate(rounds))]
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
    print('\n' + final + f'\n\n[story-ctl] everything is saved in {S}')
    sys.exit(0)

# ---------- the workflow ----------

def main(argv=None):
    global a, ROOT, S, trace, answers, step, fake_count, cur
    ap = argparse.ArgumentParser()
    ap.add_argument('story'); ap.add_argument('--base', default='HEAD'); ap.add_argument('--reply-cmd')
    ap.add_argument('--cap', type=int, default=300); ap.add_argument('--state')
    ap.add_argument('--agents', default='investigate,implement,review', help='agent names: investigate,implement,review')
    ap.add_argument('--explain-agent', default='explain', help='read-only agent that answers questions asked at a stop')
    ap.add_argument('--no-open', action='store_true', help='do not open each stop in VS Code')
    ap.add_argument('--fake', help='test mode: play back <agent>-<k>.md files from this folder instead of calling copilot')
    a = ap.parse_args(argv)
    INV, IMP, REV = a.agents.split(',')
    ROOT = os.getcwd()
    fake_count, trace, answers, step = {}, [], [], 0
    cur = {'impl': None, 'rev': None, 'verdicts': [], 'handoff': None}
    story = open(a.story).read().strip()
    criteria = parse_criteria(story)
    if not criteria:
        sys.exit('No acceptance criteria (lines starting "AC1:") found in the story file.')
    rc, out = git('rev-parse', '--verify', '--quiet', a.base + '^{commit}')
    if rc:
        sys.exit(f'--base {a.base} is not a commit in this repository (run story-ctl from the repository root).')
    base_sha = out.decode().strip()
    base_desc = base_sha if a.base == base_sha else f'{base_sha} ({a.base} when the run started)'
    if a.state:
        S = a.state
    else:
        _, gd = git('rev-parse', '--absolute-git-dir')
        S = os.path.join(gd.decode().strip() or os.path.join(ROOT, '.git'), 'story-ctl', time.strftime('%Y%m%d-%H%M%S'))
    os.makedirs(S, exist_ok=True)

    # 1. investigate, then stop for the developer
    answers_q = []
    handoff, _ = run_agent(INV, 'Story mode\n\n' + story, 'investigate', True)
    cur['handoff'] = handoff
    while True:
        r = ask(handoff + '\n\nReply go to implement, answer the questions above (implement and review will use your answers), '
                'start your reply with "Change the plan:" to have investigate plan again, or reply stop.', 1 if not answers else len(answers) + 1, 'the handoff')
        while is_question(r):
            handoff_note = answer_question(r, handoff, 'plan')
            answers_q.append((r, handoff_note))
            r = ask('Answer to your question:\n\n' + handoff_note + '\n\n---\n\n' + handoff + '\n\nReply go to implement, answer the questions above, start your reply with "Change the plan:", or reply stop.', f'h{len(answers_q)}', 'the handoff')
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
            shown = verdict_line(rev) + '\n- **Questions for the author or analyst:** ' + field(rev, 'Questions for the author or analyst')
            if v == 'Request changes':
                shown += '\n- **Edit:** ' + field(rev, 'Edit')
            tail = f'\n\nRound {n} of 3. Reply with your answers and I will continue, or reply stop.'
            r = ask(shown + tail, len(answers) + 2, f'the round {n} review questions')
            while is_question(r):
                note = answer_question(r, rev, 'review')
                r = ask('Answer to your question:\n\n' + note + '\n\n---\n\n' + shown + tail, f'q{n}-{len(answers_q)}', f'the round {n} review questions')
                answers_q.append((r, note))
            if not is_go(r):
                answers.append((f'After round {n} review', r))
            imp_prompt = f'Round {n+1} of 3\nFix the items under Edit in this review, if any, and apply the user\'s answers to its questions.\n\n{rev}{answers_block()}'
        else:
            imp_prompt = f'Round {n+1} of 3\nFix the items under Edit in this review.\n\n{rev}{answers_block()}'

if __name__ == '__main__':
    main()
