#!/usr/bin/env python3
"""story-ctl: run a whole story through investigate, implement and review, with you in the loop.

A small, deterministic workflow controller for the Copilot CLI. The three agents do the work; this script does
the bookkeeping that a model does unreliably: it passes every handoff and report on byte for byte, keeps all
your answers and gives them to every later task, reads the review verdict, counts rounds (at most 3), and stops
for you after the handoff and whenever a review has questions.

usage (from the root of the repository you are changing, for example in VS Code's terminal):
    python3 <path>/story-ctl.py story.txt [--base main]

story.txt holds the story with its acceptance criteria as lines starting "AC1:", "AC2:", ...
At each stop the agent's text is printed and opened in VS Code (press Cmd+Shift+V for the Markdown preview,
where the code links open the files). Type your reply and finish with an empty line:
  - "go" to continue, your answers, or "Change the plan: ..." at the handoff;
  - a question (ending with "?") is answered by the explain agent and you stay at the stop;
  - when implement cannot finish a round: "review", "stop", or an instruction for implement.
It stops early when a review asks for the same changes as the round before.
Everything (prompts, reports, your replies, the final report) is saved under .git/story-ctl/<time>/, outside
the working tree, so the agents never see it and nothing is committed.
Options: --base <branch> for review (default HEAD), --cap <credits per agent run> (default 300), --no-open.
Test options: --fake <folder> plays back canned agent replies; --reply-cmd answers stops from a command.
"""
import argparse, difflib, json, os, re, shutil, subprocess, sys, time

ap = argparse.ArgumentParser()
ap.add_argument('story'); ap.add_argument('--base', default='HEAD'); ap.add_argument('--reply-cmd')
ap.add_argument('--cap', type=int, default=300); ap.add_argument('--state')
ap.add_argument('--agents', default='investigate,implement,review', help='agent names: investigate,implement,review')
ap.add_argument('--explain-agent', default='explain', help='read-only agent that answers questions asked at a stop')
ap.add_argument('--no-open', action='store_true', help='do not open each stop in VS Code')
ap.add_argument('--fake', help='test mode: play back <agent>-<k>.md files from this folder instead of calling copilot')
a = ap.parse_args()
INV, IMP, REV = a.agents.split(',')
ROOT = os.getcwd()
fake_count = {}
story = open(a.story).read().strip()
criteria = '\n'.join(l.strip() for l in story.splitlines() if re.match(r'\s*AC\d+\s*:', l))
if not criteria:
    sys.exit('No acceptance criteria (lines starting "AC1:") found in the story file.')
S = a.state or os.path.join('.git', 'story-ctl', time.strftime('%Y%m%d-%H%M%S'))
os.makedirs(S, exist_ok=True)
trace = []          # for checks: ['user', text] | ['task', agent, prompt, result, tools] | ['reply', text]
answers = []        # (label, verbatim reply)
step = 0

def save(name, text):
    open(os.path.join(S, name), 'w').write(text)
    return os.path.join(S, name)

def run_agent(agent, prompt, label):
    """One fresh copilot session; returns its final message (retries once on failure)."""
    global step
    for attempt in (1, 2):
        step += 1
        pf = save(f'{step:02d}-{label}.prompt.md', prompt)
        if a.fake:
            k = fake_count[agent] = fake_count.get(agent, 0) + 1
            fp = os.path.join(a.fake, f'{agent}-{k}.md')
            final = open(fp).read().strip() if os.path.exists(fp) else ''
            if final:
                save(f'{step:02d}-{label}.result.md', final); trace.append(['task', agent, prompt, final, []]); return final
            print(f'[story-ctl] fake: no {fp}', file=sys.stderr); continue
        out = subprocess.run(['copilot', '--agent', agent, '-p', prompt, '--allow-all-tools',
                              '--max-ai-credits', str(a.cap), '--disable-client-telemetry'],
                             capture_output=True, text=True)
        log = out.stdout + out.stderr; save(f'{step:02d}-{label}.stdout.txt', log)
        sid = (re.findall(r'resume=([0-9a-f-]+)', log) or [None])[-1]
        final, tools = '', []
        ev = os.path.expanduser(f'~/.copilot/session-state/{sid}/events.jsonl') if sid else None
        if ev and os.path.exists(ev):
            for l in open(ev):
                e = json.loads(l); d = e.get('data') or {}
                if e.get('type') == 'assistant.message' and d.get('content'):
                    final = d['content']
                if e.get('type') == 'tool.execution_start':
                    tools.append(d.get('toolName'))
        final = re.sub(r'\n*\[cost:[^\]]*\]\s*$', '', final).strip()
        if final:
            save(f'{step:02d}-{label}.result.md', final)
            trace.append(['task', agent, prompt, final, tools])
            return final
        print(f'\n[story-ctl] {agent} returned nothing (attempt {attempt}); see {pf}', file=sys.stderr)
    stop_with(f'Stopped — the {agent} step failed twice; its output is in {S}.')

def ask(text, n):
    """Show a stop and get the developer's reply."""
    shown = re.sub(r'\]\((?!https?:|/|#|mailto:)([^)\s]+)\)', lambda m: f']({ROOT}/{m.group(1)})', text)
    stop_file = save(f'stop-{n}.md', shown)
    print('\n' + '=' * 78 + '\n' + text + '\n' + '=' * 78)
    print(f'[story-ctl] this stop is also in {stop_file} (Markdown preview: Cmd+Shift+V; links open the code)')
    if not a.reply_cmd and not a.no_open and shutil.which('code'):
        subprocess.run(['code', '-r', stop_file])
    if a.reply_cmd:
        r = subprocess.run(a.reply_cmd.format(stop_file=stop_file, n=n), shell=True, capture_output=True, text=True).stdout.strip()
        print(f'[reply] {r}')
    else:
        print('Your reply (empty line to finish):')
        lines = []
        for l in sys.stdin:
            if not l.strip() and lines: break
            lines.append(l.rstrip('\n'))
        r = '\n'.join(lines).strip()
    trace.append(['reply', text]); trace.append(['user', r]); save(f'reply-{n}.md', r)
    return r

def answers_block():
    if not answers: return ''
    return ('\n\nAnswers from the user (verbatim; a later answer replaces an earlier one on the same point):\n'
            + '\n'.join(f'{lab}:\n{txt}' for lab, txt in answers))

def field(text, name):
    m = re.search(r'\*\*' + re.escape(name) + r'[^*]*:\*\*(.*?)(?=\n- \*\*|\nFor implement|\Z)', text, re.S)
    return m.group(1).strip() if m else ''

def verdict(r):
    m = re.search(r'Verdict:\*\*\s*(Approve|Request changes|Needs answers)', r)
    return m.group(1) if m else None

def verdict_line(r):
    m = re.search(r'^- \*\*Verdict:\*\*.*$', r, re.M)
    return m.group(0) if m else '(no verdict line)'

def has_questions(r):
    q = re.sub(r'\s+', ' ', field(r, 'Questions for the author or analyst'))
    return bool(q) and not re.match(r'^(none|n/a)\b', q, re.I)

def is_question(r):
    t = re.sub(r'\s+', ' ', r).strip()
    if not t or re.fullmatch(r'go\.?', t, re.I): return False
    parts = [p.strip() for p in re.findall(r'[^.!?]+[.!?]*', t) if p.strip()]
    return bool(parts) and all(p.endswith('?') for p in parts)

def answer_question(q, context, what):
    ans = run_agent(a.explain_agent, f'A developer reviewing this {what} asks (verbatim): {q}\n\nAnswer the question from the code and the {what} below. Do not change anything, and do not plan new work.\n\n{what.capitalize()}:\n{context}', 'explain-question')
    return ans

def impl_incomplete(rep):
    if 'Report (implement' not in rep: return 'the implement report is missing or cut off'
    m = re.search(r'circuit breaker|attempt limit|ask (?:the user|you) how to proceed|please advise how to proceed|could not (?:compile|run (?:the )?tests)|tests could not run|build (?:is )?(?:broken|failing)|BUILD FAILURE', rep, re.I)
    return m.group(0) if m else ''

def edit_norm(r):
    e = field(r, 'Edit')
    e = re.sub(r'\]\([^)]*\)', ']', e); e = re.sub(r'[\d`*]', '', e)
    return re.sub(r'\s+', ' ', e).strip().lower()

def stop_with(result, last_impl=None, last_rev=None, rounds=()):
    rep = [f'# Story (story-ctl)', f'- **Result:** {result}',
           f'- **Rounds:** {len(rounds)} implement + review rounds; ' + '; '.join(f'round {i+1}: {v}' for i, v in enumerate(rounds))]
    if last_rev:
        rep += ['- **Criteria:**\n' + field(last_rev, 'Criteria'),
                '- **Open items:**', '  - Findings: ' + (field(last_rev, 'Findings') or 'none'),
                '  - Questions: ' + (field(last_rev, 'Questions for the author or analyst') or 'none'),
                '  - Not verified / risks: ' + (field(last_rev, 'Not verified / risks') or 'none')]
    hq = field(handoff, 'Questions for the analyst') if handoff else ''
    if hq and not re.match(r'^(none|n/a)\b', hq, re.I):
        rep += ['  - Handoff questions (check that each was answered, or that its assumption is what you want):\n' + hq]
    rep += ['  - Your answers so far:' + (answers_block() or ' none')]
    if last_impl: rep += ['\n## Last implement report\n', last_impl]
    if last_rev: rep += ['\n## Last review\n', last_rev]
    final = '\n'.join(rep)
    save('final.md', final); trace.append(['reply', final])
    json.dump(trace, open(os.path.join(S, 'trace.json'), 'w'))
    print('\n' + final + f'\n\n[story-ctl] everything is saved in {S}')
    sys.exit(0)

# 1. investigate, then stop for the developer
handoff = None
answers_q = []
handoff = run_agent(INV, 'Story mode\n\n' + story, 'investigate')
while True:
    r = ask(handoff + '\n\nReply go to implement, answer the questions above (implement and review will use your answers), '
            'or start your reply with "Change the plan:" to have investigate plan again.', 1 if not answers else len(answers) + 1)
    if is_question(r):
        handoff_note = answer_question(r, handoff, 'plan')
        answers_q.append((r, handoff_note))
        r2 = ask('Answer to your question:\n\n' + handoff_note + '\n\n---\n\n' + handoff + '\n\nReply go to implement, answer the questions above, or start your reply with "Change the plan:".', 1 + len(answers_q))
        r = r2
        if is_question(r): continue
    if re.match(r'\s*change the plan\b', r, re.I):
        handoff = run_agent(INV, f'Story mode\n\n{story}\n\nPrevious handoff:\n{handoff}\n\nChanges from the user (verbatim):\n{r}', 'reinvestigate')
        continue
    if not re.fullmatch(r'\s*go\.?\s*', r, re.I):
        answers.append(('At the handoff', r))
    break

# 2. rounds
rev_task = f'Review the changes on the current branch against base {a.base}\n\nAcceptance criteria (from the story, verbatim):\n{criteria}\n\nImplement report:\n'
imp_prompt = 'Round 1 of 3\nImplement the task based on this handoff.\n\n' + handoff + answers_block()
verdicts, impl, rev = [], None, None
prev_edit = None
for n in (1, 2, 3):
    impl = run_agent(IMP, imp_prompt, f'implement-r{n}')
    why = impl_incomplete(impl)
    if why:
        r = ask(f'Implement could not finish round {n} ({why}). Its report:\n\n{impl}\n\nReply "review" to send the work to review anyway, "stop" to stop here, or tell implement what to do next.', f'impl{n}')
        if re.fullmatch(r'\s*stop\.?\s*', r, re.I):
            stop_with(f'Stopped by you after implement could not finish round {n} ({why})', impl, rev, verdicts)
        if not re.fullmatch(r'\s*review\.?\s*', r, re.I):
            answers.append((f'During round {n} (to implement)', r))
            impl = run_agent(IMP, f'Round {n} of 3\nContinue this round. Your previous report is below; the developer\'s instruction is the last answer.\n\n{impl}{answers_block()}', f'implement-r{n}b')
            why2 = impl_incomplete(impl)
            if why2:
                stop_with(f'Stopped — implement could not finish round {n} twice ({why2})', impl, rev, verdicts)
    inv = field(handoff, 'Behavioural invariants')
    inv_block = '' if (not inv or re.match(r'^(none|n/a)\b', inv, re.I)) else '\n\nBehavioural invariants from the handoff (verbatim):\n' + inv
    rev = run_agent(REV, rev_task + impl + answers_block() + inv_block, f'review-r{n}')
    v = verdict(rev); verdicts.append(verdict_line(rev).replace('- **Verdict:** ', ''))
    en = edit_norm(rev)
    if v == 'Request changes' and prev_edit and en and difflib.SequenceMatcher(None, prev_edit, en).ratio() >= 0.9:
        stop_with(f'Stopped early — the round {n} review asks for the same changes as round {n-1}, so another round is unlikely to help; see the last review', impl, rev, verdicts)
    prev_edit = en
    if v == 'Approve':
        stop_with('Approved', impl, rev, verdicts)
    if n == 3:
        stop_with('Not approved after 3 rounds' if v == 'Request changes' else (v or 'No verdict') + ' after 3 rounds', impl, rev, verdicts)
    if v == 'Needs answers' or (v == 'Request changes' and has_questions(rev)) or v is None:
        shown = verdict_line(rev) + '\n- **Questions for the author or analyst:** ' + field(rev, 'Questions for the author or analyst')
        if v == 'Request changes':
            shown += '\n- **Edit:** ' + field(rev, 'Edit')
        r = ask(shown + f'\n\nRound {n} of 3. Reply with your answers and I will continue.', len(answers) + 2)
        while is_question(r):
            note = answer_question(r, rev, 'review')
            r = ask('Answer to your question:\n\n' + note + '\n\n---\n\n' + shown + f'\n\nRound {n} of 3. Reply with your answers and I will continue.', f'q{n}-{len(answers_q)}')
            answers_q.append((r, note))
        answers.append((f'After round {n} review', r))
        imp_prompt = f'Round {n+1} of 3\nFix the items under Edit in this review, if any, and apply the user\'s answers to its questions.\n\n{rev}{answers_block()}'
    else:
        imp_prompt = f'Round {n+1} of 3\nFix the items under Edit in this review.\n\n{rev}{answers_block()}'
