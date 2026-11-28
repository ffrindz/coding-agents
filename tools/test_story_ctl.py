#!/usr/bin/env python3
"""Offline tests for story-ctl.py: no Copilot, no credits. Run: python3 tools/test_story_ctl.py

Each workflow test makes a throwaway git repository, plays back canned agent reports (--fake) and answers the
stops from a queue (--reply-cmd), then checks the exact agent call sequence, the prompts and the final result.
"""
import importlib.util, json, os, shutil, subprocess, sys, tempfile, textwrap, time, unittest, warnings

HERE = os.path.dirname(os.path.abspath(__file__))
CTL = os.path.join(HERE, 'story-ctl.py')
spec = importlib.util.spec_from_file_location('story_ctl', CTL)
sc = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(sc)
except SystemExit:      # a controller that runs on import (before v1.1)
    sc = None

STORY = 'Story: add a thing\n\nAC1: the list shows the thing\nAC2: an unknown id returns 404\n'
JIRA = textwrap.dedent('''\
    PAY-123 Show recent transfers

    As a customer I want to see my recent transfers.

    h3. Acceptance Criteria
    * The list shows the last 10 transfers
    ** newest first
    * An unknown account id returns 404

    h3. Notes
    Design is in Figma.''')
HANDOFF = textwrap.dedent('''\
    # Handoff (investigate)
    - **Summary:** story — add a thing
    - **Questions for the analyst (story only):** Q1 should the thing be sorted? — **If unanswered:** keep today's order
    - **Plan (story only):** 1. add it''')
def events(*evs):
    return '\n'.join(json.dumps(e) for e in evs) + '\n'
def tool(i, name, args): return {'type': 'tool.execution_start', 'data': {'toolCallId': i, 'toolName': name, 'arguments': args}}
def done(i, code=None, ok=True):
    d = {'toolCallId': i, 'success': ok}
    if code is not None: d['shellExecution'] = {'exitCode': code}
    return {'type': 'tool.execution_complete', 'data': d}
IMPL_EVENTS = events(
    {'type': 'session.model_change', 'data': {'source': 'startup', 'newModel': 'claude-sonnet-5.5', 'reasoningEffort': None}},
    {'type': 'session.model_change', 'data': {'source': 'agent', 'newModel': 'gpt-6.1-sol', 'reasoningEffort': None}},
    {'type': 'session.usage_record', 'data': {'usage': {'model': 'gpt-6.1-sol', 'modelDisplayName': 'GPT-6.1 Sol', 'reasoningEffort': 'medium'}}},
    {'type': 'assistant.message', 'data': {'content': 'Checking the controller first.', 'toolRequests': [{}]}},
    tool('1', 'view', {'path': '/repo/Foo.java'}), done('1'), tool('2', 'view', {'path': 'Bar.java'}), done('2'),
    tool('3', 'report_intent', {'intent': 'x'}),
    tool('4', 'apply_patch', '*** Begin Patch\n*** Update File: Foo.java\n@@\n-a\n+b\n*** Add File: FooTest.java\n+x\n*** End Patch'),
    done('4'), tool('5', 'bash', {'command': './mvnw test -Dtest=FooTest\necho more'}), done('5', 1),
    tool('6', 'bash', {'command': './mvnw test -Dtest=FooTest'}), done('6', 0))
IMPL = '# Report (implement v3.16)\n- **Changed:** Foo.java\n- **Tests:** targeted: ok; full: ok'

def review(verdict, questions='none', edit='none', extra=''):
    return textwrap.dedent(f'''\
        # Report (review v3.38)
        - **Verdict:** {verdict}
        - **Criteria:**
          - AC1: Met
          - AC2: Met
        - **Findings:** none
        - **Questions for the author or analyst:** {questions}
        - **Edit:** {edit}
        - **Not verified / risks:** none''') + extra

APPROVE = review('Approve — 2 of 2 criteria met; blockers: 0. Fine.')
RC_Q = review('Request changes — 1 of 2 criteria met; blockers: 1.', questions='Q1 should 404 include a body? — **If unanswered:** no body',
              edit='- `src/Foo.java:10-20` -> return 404 for an unknown id')
RC = review('Request changes — 1 of 2 criteria met; blockers: 1.', edit='- `src/Foo.java:10-20` -> return 404 for an unknown id, with a page cap of 100')
RC_SAME = review('Request changes — 1 of 2 criteria met; blockers: 1.', edit='- `src/Foo.java:12-22` -> return 404 for an unknown id, with a page cap of 100')
RC_NUM = review('Request changes — 1 of 2 criteria met; blockers: 1.', edit='- `src/Foo.java:12-22` -> return 404 for an unknown id, with a page cap of 50')
NEEDS = review('Needs answers — 1 of 2 criteria met; blockers: 0.', questions='Q2 which roles may see it? — **If unanswered:** everyone')

REPLY = '''import json, sys
q = sys.argv[1]; items = json.load(open(q))
if not items: sys.exit(3)
x = items.pop(0); json.dump(items, open(q, 'w'))
if isinstance(x, dict): print(x.get('out', '')); sys.exit(x['exit'])
print(x)
'''

class Flow(unittest.TestCase):
    def setUp(self):
        warnings.simplefilter('ignore', ResourceWarning)
        self.t = tempfile.mkdtemp(); self.repo = os.path.join(self.t, 'repo'); os.mkdir(self.repo)
        self.fake = os.path.join(self.t, 'fake'); os.mkdir(self.fake)
        self.state = os.path.join(self.t, 'state')
        g = lambda *a: subprocess.run(['git', *a], cwd=self.repo, check=True, capture_output=True)
        g('init', '-q'); g('config', 'user.email', 't@example.com'); g('config', 'user.name', 't')
        open(os.path.join(self.repo, 'Foo.java'), 'w').write('class Foo {}\n')
        open(os.path.join(self.repo, '.gitignore'), 'w').write('target/\n')
        g('add', '.'); g('commit', '-qm', 'init')
        self.sha = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=self.repo, capture_output=True, text=True).stdout.strip()
        open(os.path.join(self.t, 'story.txt'), 'w').write(STORY)
        open(os.path.join(self.t, 'reply.py'), 'w').write(REPLY)
        self.log = os.path.join(self.t, 'runs.jsonl')
        self.env = dict(os.environ, STORY_CTL_LOG=self.log, STORY_CTL_NOTIFY='0')

    def tearDown(self):
        shutil.rmtree(self.t)

    def agents(self, **files):
        """files: name -> text; name like 'review_2' becomes review-2.md, 'review_2_sh' becomes review-2.sh."""
        for k, v in files.items():
            parts = k.split('_')
            ext = '.' + parts[2] if len(parts) > 2 else '.md'
            open(os.path.join(self.fake, f'{parts[0]}-{parts[1]}{ext}'), 'w').write(v)

    def run_ctl(self, replies=None, extra=(), stdin=None, state=True):
        args = [sys.executable, CTL, os.path.join(self.t, 'story.txt'), '--fake', self.fake, '--no-open', *extra]
        if state: args += ['--state', self.state]
        if replies is not None:
            qf = os.path.join(self.t, 'q.json'); json.dump(replies, open(qf, 'w'))
            args += ['--reply-cmd', f'{sys.executable} {os.path.join(self.t, "reply.py")} {qf}']
        p = subprocess.run(args, cwd=self.repo, capture_output=True, text=True, env=self.env,
                           stdin=stdin if stdin is not None else subprocess.DEVNULL)
        self.out = p.stdout + p.stderr
        self.rc = p.returncode
        tf = os.path.join(self.state, 'trace.json')
        self.trace = json.load(open(tf)) if os.path.exists(tf) else []
        self.final = open(os.path.join(self.state, 'final.md')).read() if os.path.exists(os.path.join(self.state, 'final.md')) else ''
        return [x[1] for x in self.trace if x[0] == 'task']

    def prompts(self, agent):
        return [x[2] for x in self.trace if x[0] == 'task' and x[1] == agent]

    # --- fix 1: approval and cancellation ---
    def test_answers_then_approve(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=APPROVE)
        seq = self.run_ctl(['Q1: yes, sorted by name'])
        self.assertEqual(seq, ['investigate', 'implement', 'review'])
        self.assertIn('Result:** Approved', self.final)
        self.assertIn('Q1: yes, sorted by name', self.prompts('implement')[0])
        self.assertIn('Q1: yes, sorted by name', self.prompts('review')[0])

    def test_go_carries_no_answer(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=APPROVE)
        self.run_ctl(['go'])
        self.assertNotIn('Answers from the user', self.prompts('implement')[0])

    def test_non_answers_never_start_implement(self):
        for replies in (['stop'], ['Stop.'], [''], [{'exit': 1, 'out': 'go'}], []):
            with self.subTest(replies=replies):
                shutil.rmtree(self.state, ignore_errors=True)
                self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=APPROVE)
                seq = self.run_ctl(replies)
                self.assertEqual(seq, ['investigate'], self.out)
                self.assertIn('Stopped at the handoff', self.final)

    def test_end_of_input_stops(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=APPROVE)
        self.assertEqual(self.run_ctl(None, stdin=subprocess.DEVNULL), ['investigate'])
        self.assertIn('no reply', self.final)

    def test_two_questions_stay_paused(self):
        self.agents(investigate_1=HANDOFF, explain_1='It sorts by id.', explain_2='Yes, 404 today.',
                    implement_1=IMPL, review_1=APPROVE)
        seq = self.run_ctl(['How is it sorted today?', 'Does an unknown id give 404?', 'Q1: by name'])
        self.assertEqual(seq, ['investigate', 'explain', 'explain', 'implement', 'review'])
        self.assertIn('Q1: by name', self.prompts('implement')[0])

    def test_stop_at_review_question(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=RC_Q)
        self.assertEqual(self.run_ctl(['go', 'stop']), ['investigate', 'implement', 'review'])
        self.assertIn('Stopped at the round 1 review questions', self.final)

    # --- the reported bug: questions after round 1 must stop, answers must carry ---
    def test_round1_questions_stop_and_carry(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=RC_Q, implement_2=IMPL, review_2=APPROVE)
        seq = self.run_ctl(['Q1: by name', 'Q1: no body'])
        self.assertEqual(seq, ['investigate', 'implement', 'review', 'implement', 'review'])
        users = [x[1] for x in self.trace if x[0] == 'user']
        self.assertEqual(users, ['Q1: by name', 'Q1: no body'])
        for p in self.prompts('implement')[1:] + self.prompts('review')[1:]:
            self.assertIn('Q1: by name', p); self.assertIn('Q1: no body', p)
        self.assertTrue(self.prompts('implement')[1].startswith('Round 2 of 3'))

    def test_needs_answers_stops(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=NEEDS, implement_2=IMPL, review_2=APPROVE)
        self.run_ctl(['go', 'Q2: admins only'])
        self.assertIn('Q2: admins only', self.prompts('implement')[1])

    def test_request_changes_without_questions_continues(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=RC, implement_2=IMPL, review_2=APPROVE)
        seq = self.run_ctl(['go'])
        self.assertEqual(seq.count('implement'), 2)
        self.assertEqual(len([x for x in self.trace if x[0] == 'user']), 1)

    def test_three_rounds_max(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=RC, implement_2=IMPL, review_2=RC_NUM,
                    implement_3=IMPL, review_3=RC)
        seq = self.run_ctl(['go'])
        self.assertEqual(seq.count('implement'), 3)
        self.assertIn('Not approved after 3 rounds', self.final)

    # --- fix 2: strict verdicts ---
    def test_invalid_verdicts_stop_without_edits(self):
        bad = {'missing': review('x').replace('- **Verdict:** x\n', ''),
               'unknown': review('Approve with unresolved verification'),
               'duplicate': review('Approve — 2 of 2; blockers: 0.') + '\n- **Verdict:** Request changes',
               'approve_with_blockers': review('Approve — 2 of 2 criteria met; blockers: 1.')}
        for name, rep in bad.items():
            with self.subTest(name):
                shutil.rmtree(self.state, ignore_errors=True)
                self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=rep, implement_2=IMPL)
                seq = self.run_ctl(['go', 'anything'])
                self.assertEqual(seq, ['investigate', 'implement', 'review'], name)
                self.assertIn('no valid verdict', self.final)

    # --- fix 3: early stop only without questions, numbers kept ---
    def test_same_edits_stop_early(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=RC, implement_2=IMPL, review_2=RC_SAME)
        seq = self.run_ctl(['go'])
        self.assertEqual(seq.count('implement'), 2)
        self.assertIn('Stopped early', self.final)

    def test_changed_number_is_not_same(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=RC, implement_2=IMPL, review_2=RC_NUM,
                    implement_3=IMPL, review_3=APPROVE)
        self.assertEqual(self.run_ctl(['go']).count('implement'), 3)

    def test_same_edits_with_questions_asks(self):
        rq = RC_SAME.replace('**Questions for the author or analyst:** none', '**Questions for the author or analyst:** Q3 keep the cap? — **If unanswered:** yes')
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=RC, implement_2=IMPL, review_2=rq,
                    implement_3=IMPL, review_3=APPROVE)
        seq = self.run_ctl(['go', 'Q3: yes'])
        self.assertEqual(seq.count('implement'), 3)
        self.assertIn('Q3: yes', self.prompts('implement')[2])

    # --- fix 4: read-only agents must not change the tree ---
    def test_review_edit_detected(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=APPROVE,
                    review_1_sh='echo changed >> Foo.java')
        seq = self.run_ctl(['go'])
        self.assertEqual(seq, ['investigate', 'implement', 'review'])
        self.assertIn('review step changed the working tree', self.final)
        self.assertIn('Foo.java', self.final)
        self.assertIn('changed', open(os.path.join(self.repo, 'Foo.java')).read())  # never reverted

    def test_investigate_new_file_detected_but_ignored_build_output_is_fine(self):
        self.agents(investigate_1=HANDOFF, investigate_1_sh='mkdir -p target && echo x > target/out.class',
                    implement_1=IMPL, review_1=APPROVE, review_1_sh='mkdir -p target && echo y > target/r.class')
        self.assertEqual(self.run_ctl(['go']), ['investigate', 'implement', 'review'])
        self.assertIn('Approved', self.final)
        shutil.rmtree(self.state)
        self.agents(investigate_1_sh='echo x > Notes.java')
        self.assertEqual(self.run_ctl(['go']), ['investigate'])
        self.assertIn('investigate step changed the working tree', self.final)

    def test_implement_may_edit(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, implement_1_sh='echo edit >> Foo.java', review_1=APPROVE)
        self.assertIn('Approved', (self.run_ctl(['go']), self.final)[1])

    @unittest.skipIf(sc is None, 'controller not importable')
    def test_deny_rules(self):
        self.assertIn('write', sc.READ_ONLY_DENY)
        self.assertIn('shell(git push)', sc.WRITE_DENY)
        self.assertNotIn('write', sc.WRITE_DENY)

    # --- fix 5: exit codes and no retry over partial edits ---
    def test_terminal_story_with_mixed_labels(self):
        open(os.path.join(self.t, 'story.txt'), 'w').write('Story\nAC1: a\nAC1: b\nAC 3 - c\n')
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=review('Approve — 3 of 3 criteria met; blockers: 0.'))
        self.run_ctl(['go'])
        self.assertIn('numbered again', self.out)
        self.assertIn('AC1: a\nAC2: b\nAC3: c', self.prompts('review')[0])

    def test_approve_counting_too_few_criteria(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=review('Approve — 1 of 1 criteria met; blockers: 0.'))
        self.run_ctl(['go'])
        self.assertIn('no valid verdict (Approve counting 1 criteria, but the story has 2)', self.final)

    def test_failed_review_never_approves(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=APPROVE, review_1_exit='1', review_2=APPROVE)
        self.assertEqual(self.run_ctl(['go']), ['investigate', 'implement', 'review', 'review'])
        self.assertIn('review exited with code 1, so its report is not used', self.out)
        self.assertIn('Result:** Approved', self.final)
        self.agents(review_2_exit='1'); shutil.rmtree(self.state)
        self.assertEqual(self.run_ctl(['go']), ['investigate', 'implement', 'review', 'review'])
        self.assertIn('review step failed twice', self.final); self.assertNotIn('Result:** Approved', self.final)

    def test_failed_investigate_is_not_a_plan(self):
        self.agents(investigate_1=HANDOFF, investigate_1_exit='2', investigate_2=HANDOFF, investigate_2_exit='2')
        self.assertEqual(self.run_ctl(['go']), ['investigate', 'investigate'])
        self.assertIn('investigate step failed twice', self.final)
        self.assertFalse(any(x[0] == 'reply' and 'Reply go to implement' in x[1] for x in self.trace))

    def test_plan_change_ending_in_a_question_mark(self):
        self.agents(investigate_1=HANDOFF, investigate_2=HANDOFF, implement_1=IMPL, review_1=APPROVE)
        self.assertEqual(self.run_ctl(['Change the plan: use Redis?', 'go']), ['investigate', 'investigate', 'implement', 'review'])
        self.assertIn('Change the plan: use Redis?', self.prompts('implement')[0])

    def test_damaged_journal_while_running_does_not_stop_the_agent(self):
        sh = 'printf "{broken\\n" >> "$STATE/journal.jsonl"; printf "{\\"type\\": \\"session\\", \\"seconds\\": 0}\\n" >> "$STATE/journal.jsonl"'
        self.env['STATE'] = self.state
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, implement_1_sh=sh, implement_1_events=IMPL_EVENTS, review_1=APPROVE)
        self.run_ctl(['go'])
        self.assertIn('Result:** Approved', self.final)

    def test_plan_changes_reach_every_later_task(self):
        self.agents(investigate_1=HANDOFF, investigate_2=HANDOFF, investigate_3=HANDOFF, implement_1=IMPL, review_1=APPROVE)
        self.run_ctl(['Change the plan: store it in Redis', 'Change the plan: and expire after 1 hour', 'go'])
        inv3 = self.prompts('investigate')[2]
        self.assertIn('Change the plan: store it in Redis', inv3); self.assertIn('Change the plan: and expire after 1 hour', inv3)
        for agent in ('implement', 'review'):
            self.assertIn('Change the plan: store it in Redis', self.prompts(agent)[0])
            self.assertIn('Change the plan: and expire after 1 hour', self.prompts(agent)[0])

    def test_progress_marks_a_failed_step(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, implement_1_exit='1', review_1=APPROVE)
        self.run_ctl(['go', 'stop'])
        prog = open(os.path.join(self.state, 'progress.md')).read()
        self.assertIn('⚠ exit code 1: implement round 1', prog); self.assertNotIn('✓ implement', prog)

    def test_resume_with_a_cut_off_journal_line(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=APPROVE)
        self.run_ctl(['stop'])
        open(os.path.join(self.state, 'journal.jsonl'), 'a').write('{"type": "start", "age')
        self.assertEqual(self.resume(['go']), ['investigate', 'implement', 'review'])
        self.assertIn('Approved', self.final)
        lines = open(os.path.join(self.state, 'journal.jsonl')).read().splitlines()
        lines.insert(1, '{broken'); open(os.path.join(self.state, 'journal.jsonl'), 'w').write('\n'.join(lines) + '\n')
        self.resume(['go']); self.assertIn('damaged at line 2', self.out)

    def test_odd_session_events_do_not_stop_the_agent(self):
        odd = '[1, 2]\n{"type": "tool.execution_start", "data": ["x"]}\n{"type": "tool.execution_start", "data": {"toolName": "bash", "arguments": 5}}\n' + IMPL_EVENTS
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, implement_1_events=odd, review_1=APPROVE)
        self.run_ctl(['go'])
        self.assertIn('Result:** Approved', self.final)

    def test_implement_nonzero_exit_is_incomplete(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, implement_1_exit='1', review_1=APPROVE)
        seq = self.run_ctl(['go', 'stop'])
        self.assertEqual(seq, ['investigate', 'implement'])
        self.assertIn('exited with code 1', self.final)

    def test_implement_nonzero_exit_then_review_anyway(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, implement_1_exit='1', review_1=APPROVE)
        self.assertEqual(self.run_ctl(['go', 'review']), ['investigate', 'implement', 'review'])

    def test_no_retry_after_partial_edits(self):
        self.agents(investigate_1=HANDOFF, implement_1_sh='echo half >> Foo.java', implement_2=IMPL, review_1=APPROVE)
        seq = self.run_ctl(['go'])
        self.assertEqual(seq, ['investigate', 'implement'])
        self.assertIn('not retried', self.final)

    def test_retry_when_nothing_changed(self):
        self.agents(investigate_1=HANDOFF, implement_2=IMPL, review_1=APPROVE)
        self.assertEqual(self.run_ctl(['go']), ['investigate', 'implement', 'implement', 'review'])

    # --- fix 6: multi-line criteria reach review ---
    def test_jira_story_file(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=APPROVE)
        open(os.path.join(self.t, 'story.txt'), 'w').write(JIRA)
        self.assertEqual(self.run_ctl(['go']), ['investigate', 'implement', 'review'])
        self.assertIn('Result:** Approved', self.final)
        self.assertIn('acceptance criteria numbered from the story', self.out)
        self.assertIn('AC2: An unknown account id returns 404', self.prompts('investigate')[0])
        self.assertIn('AC1: The list shows the last 10 transfers\n  - newest first\nAC2:', self.prompts('review')[0])

    def test_progress_page(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, implement_1_events=IMPL_EVENTS, review_1=APPROVE)
        self.run_ctl(['Q1: yes'])
        prog = open(os.path.join(self.state, 'progress.md')).read()
        self.assertIn('# Story: add a thing', prog); self.assertIn('**Finished**', prog)
        self.assertIn('AC2: an unknown id returns 404', prog)
        self.assertIn('✓ implement round 1 · GPT-6.1 Sol, effort medium ·', prog)
        for x in ('✓ investigate', '↩ your reply at the handoff: “Q1: yes”', '✓ implement round 1', '✓ review round 1', '■ Approved'):
            self.assertIn(x, prog)
        self.assertNotIn('doing now', prog)
        m = __import__('re').search(r'implement round 1 .*\[activity\]\((\S+)\)', prog)
        act = open(os.path.join(self.state, m.group(1))).read()
        self.assertIn('read    /repo/Foo.java, Bar.java', act); self.assertIn('edit    Foo.java, FooTest.java', act)
        self.assertIn('./mvnw test -Dtest=FooTest   ✗ exit 1', act); self.assertIn('note    Checking the controller first.', act)
        self.assertNotIn('report_intent', act)

    def test_notifications_and_final_report_opened(self):
        rec = os.path.join(self.t, 'rec.py'); calls = os.path.join(self.t, 'calls.txt')
        open(rec, 'w').write(f'import sys\nopen({calls!r}, "a").write(" | ".join(sys.argv[1:]) + "\\n")\n')
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=APPROVE)
        self.env.update(STORY_CTL_NOTIFY='1', STORY_CTL_NOTIFY_CMD=f'{sys.executable} {rec}', STORY_CTL_EDITOR=f'{sys.executable} {rec}')
        open(os.path.join(self.t, 'story.txt'), 'w').write(STORY)
        args = [sys.executable, CTL, os.path.join(self.t, 'story.txt'), '--fake', self.fake, '--state', self.state]
        p = subprocess.run(args, cwd=self.repo, input='go\n\n', capture_output=True, text=True, env=self.env)
        got = open(calls).read()
        self.assertIn('Story: Story: add a thing | The plan is ready', got)
        self.assertIn('Story: Story: add a thing | Approved', got)
        self.assertIn(os.path.join(self.state, 'final.md'), got)
        self.env['STORY_CTL_NOTIFY'] = '0'; os.remove(calls); shutil.rmtree(self.state)
        subprocess.run(args, cwd=self.repo, input='stop\n\n', capture_output=True, text=True, env=self.env)
        self.assertNotIn('Story:', open(calls).read())          # turned off; you stopped it, so final.md is not opened
        self.assertNotIn('final.md', open(calls).read())

    def test_multiline_criteria_in_review_task(self):
        open(os.path.join(self.t, 'story.txt'), 'w').write(
            'Story\n\nAC1: return HTTP 409 on a duplicate\n   Include errorType DUPLICATE_TRANSACTION\nAC2: list them\n  - sorted by date\n\nNotes: not a criterion\n')
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=APPROVE)
        self.run_ctl(['go'])
        p = self.prompts('review')[0]
        for s in ('AC1: return HTTP 409', 'Include errorType DUPLICATE_TRANSACTION', 'AC2: list them', '- sorted by date'):
            self.assertIn(s, p)
        self.assertNotIn('Notes: not a criterion', p.split('Implement report:')[0])

    # --- fix 7: pinned base and git dir ---
    def test_base_pinned_to_commit(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=APPROVE,
                    implement_1_sh='echo c >> Foo.java && git commit -qam wip')
        self.run_ctl(['go'])
        self.assertIn(f'against base {self.sha} (HEAD when the run started)', self.prompts('review')[0])

    def test_invalid_base_rejected(self):
        self.agents(investigate_1=HANDOFF)
        self.assertEqual(self.run_ctl(['go'], extra=('--base', 'no-such-branch')), [])
        self.assertNotEqual(self.rc, 0); self.assertIn('not a commit', self.out)

    def test_linked_worktree_default_state(self):
        wt = os.path.join(self.t, 'wt')
        subprocess.run(['git', 'worktree', 'add', '-q', wt], cwd=self.repo, check=True, capture_output=True)
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=APPROVE)
        qf = os.path.join(self.t, 'q.json'); json.dump(['go'], open(qf, 'w'))
        p = subprocess.run([sys.executable, CTL, os.path.join(self.t, 'story.txt'), '--fake', self.fake, '--no-open',
                            '--reply-cmd', f'{sys.executable} {os.path.join(self.t, "reply.py")} {qf}'],
                           cwd=wt, capture_output=True, text=True, stdin=subprocess.DEVNULL, env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('Approved', p.stdout)
        self.assertIn(os.path.join('.git', 'worktrees'), p.stdout)

    # --- run log, stats, report ---
    def logs(self):
        return [json.loads(l) for l in open(self.log)] if os.path.exists(self.log) else []

    def test_run_log_and_credits(self):
        self.agents(investigate_1=HANDOFF, investigate_1_credits='2.5', implement_1=IMPL, implement_1_credits='20',
                    review_1=RC, review_1_credits='10', implement_2=IMPL, implement_2_credits='5', review_2=APPROVE, review_2_credits='7.5')
        self.run_ctl(['go'])
        e = self.logs()[-1]
        self.assertEqual((e['outcome'], e['rounds'], e['credits_total']), ('approved', 2, 45.0))
        self.assertEqual(e['credits'], {'investigate': 2.5, 'implement': 25.0, 'review': 17.5})
        self.assertEqual(e['agents'], {'implement': 'v3.16', 'review': 'v3.38'})
        self.assertIn('**Credits and time:** 45 credits', self.final)
        self.assertIn('(so far: 2 credits', open(os.path.join(self.state, 'stop-1.md')).read())

    def test_no_log(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=APPROVE)
        self.run_ctl(['go'], extra=('--no-log',))
        self.assertEqual(self.logs(), [])

    def test_stats(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=APPROVE)
        self.run_ctl(['go'])
        st1 = self.state; self.state = os.path.join(self.t, 'state2')
        self.run_ctl(['stop'])
        p = subprocess.run([sys.executable, CTL, '--stats'], cwd=self.repo, capture_output=True, text=True, env=self.env)
        self.assertIn('2 runs', p.stdout); self.assertIn('approved 1', p.stdout); self.assertIn('stopped by you 1', p.stdout)
        self.assertIn('in round 1: 1', p.stdout)

    def test_report(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=RC_Q, implement_2=IMPL, review_2=APPROVE)
        self.run_ctl(['Q1: by name', 'Q1: no body'])
        p = subprocess.run([sys.executable, CTL, '--report', self.state], cwd=self.repo, capture_output=True, text=True, env=self.env)
        r = open(os.path.join(self.state, 'problem-report.md')).read()
        for x in ('implement v3.16, review v3.38', 'AC1: the list shows the thing', 'AC2: an unknown id returns 404',
                  'Stop 1 (the handoff): questions asked: Q1 should the thing be sorted?', 'you replied: Q1: by name',
                  'Stop 2 (the round 1 review questions): questions asked: Q1 should 404 include a body?', 'you replied: Q1: no body',
                  'Round 1: - **Verdict:** Request changes', 'Edit items: 1', 'Round 2: - **Verdict:** Approve', 'Edit items: 0',
                  'Result:              Approved', 'What went wrong:'):
            self.assertIn(x, r)
        self.assertIn('replace names', p.stdout)

    # --- resume ---
    def resume(self, replies, run=None):
        return self.run_ctl(replies, extra=('--resume', run or self.state), state=False)

    def test_resume_after_stop_at_handoff(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=APPROVE)
        self.assertEqual(self.run_ctl(['stop']), ['investigate'])
        self.assertIn('--resume', self.final)
        seq = self.resume(['Q1: by name'])            # investigate-2.md does not exist: a live investigate would fail
        self.assertEqual(seq, ['investigate', 'implement', 'review'])
        self.assertIn('Approved', self.final)
        self.assertIn('resumed: investigate (saved result)', self.out)
        self.assertIn('Q1: by name', self.prompts('implement')[0])
        self.assertEqual(len(self.logs()), 2); self.assertEqual(self.logs()[-1]['resumed'], 1)

    def test_resume_after_review_question_stop_keeps_answers(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=RC_Q, implement_2=IMPL, review_2=APPROVE)
        self.run_ctl(['Q1: by name', 'stop'])
        seq = self.resume(['Q1: no body'])
        self.assertEqual(seq, ['investigate', 'implement', 'review', 'implement', 'review'])
        for p in self.prompts('implement')[1:] + self.prompts('review')[1:]:
            self.assertIn('Q1: by name', p); self.assertIn('Q1: no body', p)
        self.assertIn('Approved', self.final)

    def test_resume_twice(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=RC_Q, implement_2=IMPL, review_2=APPROVE)
        self.run_ctl(['stop'])
        self.resume(['Q1: by name', 'stop'])
        seq = self.resume(['Q1: no body'])
        self.assertEqual(seq, ['investigate', 'implement', 'review', 'implement', 'review'])
        self.assertIn('Approved', self.final)

    def test_resume_after_outage(self):
        self.agents(investigate_1=HANDOFF, review_1=APPROVE)       # implement-1 and implement-2 return nothing
        self.run_ctl(['go'])
        self.assertIn('failed twice', self.final)
        self.agents(implement_3=IMPL)
        seq = self.resume([])                                       # no stop is replayed or asked
        self.assertEqual(seq, ['investigate', 'implement', 'review'])
        self.assertIn('Approved', self.final)

    def test_resume_after_interrupt_asks_before_rerunning_implement(self):
        self.agents(investigate_1=HANDOFF, implement_1_sh='echo half >> Foo.java; kill -INT $PPID; sleep 2',
                    implement_2=IMPL, review_1=APPROVE)
        self.run_ctl(['go'])
        self.assertEqual(self.rc, 130, self.out)
        self.assertIn('--resume', self.out)
        self.assertEqual(self.logs()[-1]['outcome'], 'interrupted')
        self.resume(['stop'])
        self.assertIn('Stopped at the resume of an interrupted step', self.final)
        stop = open(os.path.join(self.state, 'stop-resume.md')).read()
        self.assertIn('Foo.java', stop)
        seq = self.resume(['retry'])
        self.assertEqual(seq, ['investigate', 'implement', 'review'])
        self.assertIn('Approved', self.final)

    def test_closed_terminal_is_resumable(self):
        self.agents(investigate_1=HANDOFF, implement_1_sh='echo half >> Foo.java; kill -HUP $PPID; sleep 2',
                    implement_2=IMPL, review_1=APPROVE)
        self.run_ctl(['go'])
        self.assertEqual(self.rc, 130, self.out)
        self.assertEqual(self.logs()[-1]['outcome'], 'interrupted')
        self.assertEqual(self.resume(['retry']), ['investigate', 'implement', 'review'])
        self.assertIn('Approved', self.final)

    def test_resume_finished_run_refused(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=APPROVE)
        self.run_ctl(['go'])
        self.resume([])
        self.assertIn('already ended (Approved)', self.out)

    def test_resume_detects_changed_saved_run(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=RC_Q, implement_2=IMPL, review_2=APPROVE)
        self.run_ctl(['go', 'stop'])
        jp = os.path.join(self.state, 'journal.jsonl')
        j = [json.loads(l) for l in open(jp)]
        for e in j:
            if e['type'] == 'task' and e['label'] == 'investigate': e['result'] += '\nedited'
        open(jp, 'w').write(''.join(json.dumps(e) + '\n' for e in j))
        self.resume(['Q1: no body'])
        self.assertIn('Cannot resume', self.out)

    def test_resume_last(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=APPROVE)
        self.run_ctl(['stop'], state=False)
        seq = self.run_ctl(['go'], extra=('--resume', 'last'), state=False)
        self.assertIn('resumed: investigate (saved result)', self.out)
        self.assertIn('Approved', self.out)


class Chat(unittest.TestCase):
    """The story agent's flow: hook keeps the message, --chat starts or continues the run in the background."""
    def setUp(self):
        warnings.simplefilter('ignore', ResourceWarning)
        self.t = tempfile.mkdtemp(); self.repo = os.path.join(self.t, 'repo'); os.mkdir(self.repo)
        self.fake = os.path.join(self.t, 'fake'); os.mkdir(self.fake)
        g = lambda *a: subprocess.run(['git', *a], cwd=self.repo, check=True, capture_output=True)
        g('init', '-q'); g('config', 'user.email', 't@example.com'); g('config', 'user.name', 't')
        open(os.path.join(self.repo, 'Foo.java'), 'w').write('class Foo {}\n'); g('add', '.'); g('commit', '-qm', 'init')
        self.home = os.path.join(self.t, 'sc-home'); self.uhome = os.path.join(self.t, 'home'); os.mkdir(self.uhome)
        self.env = dict(os.environ, STORY_CTL_NOTIFY='0', STORY_CTL_HOME=self.home, STORY_CTL_LOG=os.path.join(self.t, 'runs.jsonl'),
                        HOME=self.uhome, COPILOT_AGENT_SESSION_ID='sid-1')
        self.env.pop('STORY_CTL_EDITOR', None)

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    def agents(self, **files):
        for k, v in files.items():
            parts = k.split('_'); ext = '.' + parts[2] if len(parts) > 2 else '.md'
            open(os.path.join(self.fake, f'{parts[0]}-{parts[1]}{ext}'), 'w').write(v)

    def hook(self, msg, sid='sid-1', cwd=None):
        p = subprocess.run([sys.executable, CTL, '--hook-prompt'], input=json.dumps({'sessionId': sid, 'prompt': msg, 'cwd': cwd or self.repo,
                           'timestamp': time.time()}), capture_output=True, text=True, env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr); return p.stdout

    def chat(self, msg, sid='sid-1', hook=True):
        if hook: self.hook(msg, sid)
        time.sleep(0.01)
        env = dict(self.env, COPILOT_AGENT_SESSION_ID=sid)
        p = subprocess.run([sys.executable, CTL, '--chat', '--fake', self.fake, '--no-open'], cwd=self.repo,
                           capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.strip()

    def run_dir(self, sid='sid-1'):
        return json.load(open(os.path.join(self.home, 'chats', sid + '.json')))['run']

    def wait(self, sid='sid-1', states=('paused', 'ended', 'interrupted')):
        r = self.run_dir(sid)
        for _ in range(300):
            try:
                st = json.load(open(os.path.join(r, 'status.json')))
                if st['state'] in states and not sc_alive(st.get('pid')): return st
            except (OSError, ValueError): pass
            time.sleep(0.1)
        self.fail('run did not reach ' + str(states) + ': ' + open(os.path.join(r, 'ctl.log')).read()[-2000:])

    def journal(self, sid='sid-1'):
        return [json.loads(l) for l in open(os.path.join(self.run_dir(sid), 'journal.jsonl'))]

    def tasks(self, sid='sid-1'):
        return [e for e in self.journal(sid) if e['type'] == 'task']

    def prompt(self, label, sid='sid-1'):
        d = self.run_dir(sid)
        return open(os.path.join(d, next(f for f in sorted(os.listdir(d)) if f.endswith(f'-{label}.prompt.md'))), newline='').read()

    def test_full_story_through_chat_keeps_replies_verbatim(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=RC_Q, implement_2=IMPL, review_2=APPROVE)
        out = self.chat(STORY)
        self.assertIn('Started', out)
        st = self.wait(); self.assertEqual(st['state'], 'paused'); self.assertEqual(st['where'], 'the handoff')
        self.assertTrue(os.path.exists(st['stop_file']))
        self.assertIn('Waiting for your reply at the handoff', self.chat('status'))
        r1 = 'Q1: sort by "name", then $id\n  - and keep `nulls` last'
        self.assertIn('Got it', self.chat(r1))
        st = self.wait(); self.assertEqual(st['where'], 'the round 1 review questions')
        self.chat('Q1: no body')
        st = self.wait(); self.assertEqual(st['state'], 'ended'); self.assertEqual(st['result'], 'Approved')
        replies = [e['text'] for e in self.journal() if e['type'] == 'reply']
        self.assertEqual(replies, [r1, 'Q1: no body'])
        self.assertEqual([t['label'] for t in self.tasks()], ['investigate', 'implement-r1', 'review-r1', 'implement-r2', 'review-r2'])
        tr = json.load(open(os.path.join(self.run_dir(), 'trace.json')))
        impl2 = [x[2] for x in tr if x[0] == 'task' and x[1] == 'implement'][1]
        self.assertIn(r1, impl2); self.assertIn('Q1: no body', impl2)
        self.assertIn('Finished: Approved', self.chat('status'))
        self.assertIn('Start a new chat', self.chat('anything else'))

    def test_question_at_a_stop(self):
        self.agents(investigate_1=HANDOFF, explain_1='It sorts by id today.', implement_1=IMPL, review_1=APPROVE)
        self.chat(STORY); self.wait()
        self.chat('How is it sorted today?')
        st = self.wait(); self.assertEqual((st['state'], st['where']), ('paused', 'the handoff'))
        self.assertIn('It sorts by id today.', open(st['stop_file']).read())
        self.chat('go'); self.assertEqual(self.wait()['result'], 'Approved')

    def test_message_while_running_is_not_used(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, implement_1_sh='sleep 4', implement_1_events=IMPL_EVENTS, review_1=APPROVE)
        self.chat(STORY); self.wait()
        self.chat('go'); time.sleep(1.5)
        out = self.chat('Q1: actually sort by date')
        self.assertIn('Working: implement round 1', out); self.assertIn('was not used', out)
        self.assertIn('./mvnw test -Dtest=FooTest   ✓', out); self.assertIn('progress.md', out)
        prog = open(os.path.join(self.run_dir(), 'progress.md')).read()
        self.assertIn('▶ implement round 1 · GPT-6.1 Sol, effort medium · running', prog)
        self.assertRegex(prog, r'\*\*Working: implement round 1\*\* · .* · updated \d\d:\d\d:\d\d\n')
        self.assertIn('## What implement is doing now (GPT-6.1 Sol, effort medium)', prog)
        self.assertIn('Working: implement round 1 on GPT-6.1 Sol, effort medium', out)
        self.assertEqual(self.wait()['result'], 'Approved')
        self.assertNotIn('sort by date', open(os.path.join(self.run_dir(), 'trace.json')).read())

    def test_stop_while_running_then_resume(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, implement_1_sh='echo half >> Foo.java; sleep 20',
                    implement_2=IMPL, review_1=APPROVE)
        self.chat(STORY); self.wait(); self.chat('go'); time.sleep(1.5)
        self.assertIn('Stopping', self.chat('stop'))
        st = self.wait(); self.assertEqual(st['state'], 'interrupted')
        self.assertIn('interrupted during implement-r1', self.chat('status'))
        self.assertIn('Resuming', self.chat('resume'))
        st = self.wait(); self.assertEqual((st['state'], st['where']), ('paused', 'the resume of an interrupted step'))
        self.assertIn('Foo.java', open(st['stop_file']).read())
        self.chat('retry'); self.assertEqual(self.wait()['result'], 'Approved')

    def test_same_message_twice_runs_once(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=APPROVE)
        self.chat(STORY); self.wait()
        self.hook('go'); a1 = self.chat('go', hook=False); a2 = self.chat('go', hook=False)
        self.assertEqual(a1, a2)
        self.assertEqual(self.wait()['result'], 'Approved')
        self.assertEqual([t['label'] for t in self.tasks()].count('implement-r1'), 1)

    def test_new_chat_is_a_new_story(self):
        self.agents(investigate_1=HANDOFF, investigate_2=HANDOFF, implement_1=IMPL, review_1=APPROVE)
        self.chat(STORY); self.wait()
        self.chat('go'); self.assertEqual(self.wait()['result'], 'Approved')
        self.assertIn('Started', self.chat(STORY, sid='sid-2')); self.wait('sid-2')
        self.assertNotEqual(self.run_dir('sid-1'), self.run_dir('sid-2'))
        self.assertEqual(self.wait('sid-2')['state'], 'paused')

    def test_one_story_at_a_time_per_working_tree(self):
        self.agents(investigate_1=HANDOFF, investigate_2=HANDOFF)
        self.chat(STORY); self.wait(); first = self.run_dir()
        out = self.chat(STORY, sid='sid-2')
        self.assertIn('Another story in this repository is waiting for a reply at the handoff', out)
        self.assertFalse(os.path.exists(os.path.join(self.home, 'chats', 'sid-2.json')) and
                         json.load(open(os.path.join(self.home, 'chats', 'sid-2.json'))).get('run'))
        self.assertIn('Started', self.chat('set aside', sid='sid-2')); self.wait('sid-2')
        self.assertEqual(sc_status(first)['state'], 'ended')
        self.assertIn('resume', self.chat('status'))                           # the set-aside run stays resumable
        self.assertIn('Another story in this repository is paused', self.chat('resume'))   # not while the new one is active
        self.chat('stop', sid='sid-2'); self.wait('sid-2', states=('ended',))
        self.assertIn('Resuming', self.chat('resume'))
        st = self.wait()
        self.assertEqual(st['state'], 'paused'); self.assertEqual(st['where'], 'the handoff')   # back at the stop it was waiting at

    def test_repository_moved_while_paused(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=APPROVE)
        self.chat(STORY); self.wait()
        g = lambda *a: subprocess.run(['git', *a], cwd=self.repo, check=True, capture_output=True)
        g('commit', '-q', '--allow-empty', '-m', 'meanwhile')
        self.chat('go'); st = self.wait()
        self.assertIn('moved from commit', open(st['stop_file']).read())
        g('checkout', '-qb', 'other')
        self.chat('go now'); st = self.wait()
        self.assertEqual(st['state'], 'paused')
        self.assertIn('Your reply was not used:** the branch changed from', open(st['stop_file']).read())
        self.assertNotIn('implement', [t['agent'] for t in self.tasks()])
        self.chat('go again'); self.assertEqual(self.wait(states=('ended',))['result'], 'Approved')

    def test_stop_while_starting(self):
        run = os.path.join(self.t, 'r'); os.makedirs(run)
        json.dump({'state': 'starting', 'pid': None, 'updated': time.time()}, open(os.path.join(run, 'status.json'), 'w'))
        self.assertIn('just starting', sc_chat_reply(self, run, 'stop'))

    def test_two_chats_start_at_the_same_moment(self):
        self.agents(investigate_1=HANDOFF, investigate_2=HANDOFF)
        self.hook(STORY, 'sid-a'); self.hook(STORY, 'sid-b')
        env = lambda sid: dict(self.env, COPILOT_AGENT_SESSION_ID=sid)
        ps = [subprocess.Popen([sys.executable, CTL, '--chat', '--fake', self.fake, '--no-open'], cwd=self.repo, env=env(x),
                               stdout=subprocess.PIPE, text=True) for x in ('sid-a', 'sid-b')]
        outs = [p.communicate(timeout=60)[0] for p in ps]
        self.assertEqual(sum('Started' in o for o in outs), 1, outs)
        self.assertEqual(sum('Another story' in o for o in outs), 1, outs)

    def test_notification_comes_after_the_saved_state(self):
        rec = os.path.join(self.t, 'rec.py'); out = os.path.join(self.t, 'seen')
        open(rec, 'w').write('import glob, json, os, sys\n'
                             f'st = [json.load(open(f))["state"] for f in glob.glob({self.repo!r} + "/.git/story-ctl/*/status.json")]\n'
                             f'open({out!r}, "a").write(",".join(st) + "\\n")\n')
        self.env.update(STORY_CTL_NOTIFY='1', STORY_CTL_NOTIFY_CMD=f'{sys.executable} {rec}')
        self.agents(investigate_1=HANDOFF)
        self.chat(STORY); self.wait()
        self.assertEqual(open(out).read().split(), ['paused'])

    def test_reply_kept_exactly_as_typed(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=APPROVE)
        self.chat(STORY); self.wait()
        reply = '  Q1: use this:\r\n      if (x) {\n          y();\n      }\n'
        self.chat(reply); self.wait(states=('ended',))
        self.assertIn(reply, self.prompt('implement-r1'))

    def test_stop_at_a_stop_then_resume_in_a_new_chat(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=APPROVE)
        self.chat(STORY); self.wait()
        self.chat('stop'); st = self.wait(); self.assertTrue(st['resumable'])
        self.assertIn('Resuming', self.chat('resume', sid='sid-3'))
        st = self.wait('sid-3'); self.assertEqual(st['where'], 'the handoff')
        self.chat('go', sid='sid-3'); self.assertEqual(self.wait('sid-3')['result'], 'Approved')

    def test_help_and_no_criteria(self):
        self.assertIn('could not find acceptance criteria', self.chat('hello'))
        self.assertIn('Paste a story', self.chat('help'))
        self.assertFalse(os.path.exists(os.path.join(self.home, 'chats', 'sid-1.json')) and json.load(open(os.path.join(self.home, 'chats', 'sid-1.json'))).get('run'))

    def test_jira_paste_is_numbered_and_started(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=APPROVE)
        out = self.chat(JIRA)
        self.assertIn('Started with these acceptance criteria', out)
        self.assertIn('AC1: The list shows the last 10 transfers', out); self.assertIn('AC2: An unknown account id returns 404', out)
        st = self.wait()
        self.assertEqual(st['state'], 'paused')
        story = open(os.path.join(self.run_dir(), 'story.txt')).read()
        self.assertIn('PAY-123 Show recent transfers', story); self.assertIn('Design is in Figma.', story)
        self.assertEqual(open(os.path.join(self.run_dir(), 'story-pasted.txt')).read(), JIRA + '\n')
        self.assertIn('AC2: An unknown account id returns 404', self.prompt('investigate'))
        self.chat('go'); self.wait(states=('ended',))
        rev = self.prompt('review-r1')
        self.assertIn('AC1: The list shows the last 10 transfers\n  - newest first\nAC2: An unknown account id returns 404', rev)

    def test_plain_lines_are_confirmed_first(self):
        self.agents(investigate_1=HANDOFF)
        out = self.chat('The list shows 10 transfers\nAn unknown id returns 404')
        self.assertIn('Reply `go` to start', out); self.assertIn('AC2: An unknown id returns 404', out)
        self.assertNotIn('run', json.load(open(os.path.join(self.home, 'chats', 'sid-1.json'))))
        self.assertIn('Started', self.chat('go'))
        self.assertEqual(self.wait()['state'], 'paused')
        self.assertIn('AC1: The list shows 10 transfers', self.prompt('investigate'))

    def test_paste_again_after_stop_starts_a_new_run(self):
        self.agents(investigate_1=HANDOFF, investigate_2=HANDOFF)
        self.chat(JIRA); self.wait(); first = self.run_dir()
        self.chat('stop'); self.wait(states=('ended',))
        self.assertIn('Started', self.chat(STORY))
        self.assertNotEqual(self.run_dir(), first)

    def test_no_hook_falls_back_to_session_log_or_explains(self):
        self.agents(investigate_1=HANDOFF)
        self.assertIn('could not read your message', self.chat('x', sid='sid-9', hook=False))
        d = os.path.join(self.uhome, '.copilot', 'session-state', 'sid-9'); os.makedirs(d)
        open(os.path.join(d, 'events.jsonl'), 'w').write(json.dumps({'type': 'user.message', 'id': 'e1', 'data': {'content': STORY}}) + '\n')
        self.assertIn('Started', self.chat('ignored', sid='sid-9', hook=False))

    def test_outside_a_repository(self):
        os.makedirs(os.path.join(self.t, 'plain'))
        self.hook(STORY, cwd=os.path.join(self.t, 'plain'))
        p = subprocess.run([sys.executable, CTL, '--chat'], cwd=os.path.join(self.t, 'plain'), capture_output=True, text=True, env=self.env)
        self.assertIn('Open the repository', p.stdout)

    def test_stats_and_report_in_chat(self):
        self.agents(investigate_1=HANDOFF, implement_1=IMPL, review_1=APPROVE)
        self.chat(STORY); self.wait(); self.chat('go'); self.wait()
        self.assertIn('1 runs', self.chat('stats'))
        out = self.chat('report'); self.assertIn('problem-report.md', out)
        self.assertTrue(os.path.exists(os.path.join(self.run_dir(), 'problem-report.md')))

    def guard(self, tool, args):
        p = subprocess.run([sys.executable, CTL, '--hook-guard'], input=json.dumps({'sessionId': 's', 'toolName': tool, 'toolArgs': args}),
                           capture_output=True, text=True, env=self.env)
        return json.loads(p.stdout).get('permissionDecision', 'allow')

    def test_guard(self):
        ok = ['python3 ~/.story-ctl/story-ctl.py --chat', f'python3 -I "{self.uhome}/.story-ctl/story-ctl.py" --chat',
'python3 ~/.story-ctl/story-ctl.py --chat 2>&1',
              'cd /repo && python3 ~/.story-ctl/story-ctl.py --chat']
        for c in ok:
            self.assertEqual(self.guard('bash', {'command': c}), 'allow', c)
        win = 'py "$env:USERPROFILE\\.story-ctl\\story-ctl.py" --chat'      # bash would run ":USERPROFILE\..." from the repository
        self.assertEqual(self.guard('bash', {'command': win}), 'allow' if os.name == 'nt' else 'deny')
        bad = ['python3 ~/.story-ctl/story-ctl.py --chat; rm -rf src', 'python3 ~/.story-ctl/story-ctl.py --chat && git push',
               'cat src/Foo.java', 'python3 ~/.story-ctl/story-ctl.py story.txt', 'python3 evil.py --chat',
               'python3 ~/.story-ctl/story-ctl.py --chat | tee x', 'python3 ./story-ctl.py --chat',
               'python3 /tmp/x/.story-ctl/story-ctl.py --chat', 'python3 ~/.story-ctl/../x/story-ctl.py --chat',
               'python3 "~/.story-ctl/story-ctl.py" --chat', "python3 '~/.story-ctl/story-ctl.py' --chat",
               "python3 '$HOME/.story-ctl/story-ctl.py' --chat", 'python3 ~x/.story-ctl/story-ctl.py --chat',
               'python3 "$HOME/.story-ctl\\story-ctl.py" --chat']
        self.assertEqual(self.guard('bash', {'command': 'python3 $HOME/.story-ctl/story-ctl.py --chat'}), 'allow')
        for c in bad:
            self.assertEqual(self.guard('bash', {'command': c}), 'deny', c)
        self.assertEqual(self.guard('edit', {'path': 'a', 'new_str': 'b'}), 'deny')
        self.assertEqual(self.guard('skill', {'skill': 'x'}), 'deny')
        self.assertEqual(self.guard('read_bash', {'shellId': '0'}), 'allow')
        p = subprocess.run([sys.executable, CTL, '--hook-guard'], input=json.dumps({'tool_name': 'Bash', 'tool_input': {'command': 'ls'}}),
                           capture_output=True, text=True, env=self.env)
        self.assertEqual(json.loads(p.stdout)['permissionDecision'], 'deny')

    def test_hook_prompt_verbatim(self):
        m = 'Line "one" $HOME `x`\n\n  indented\ttab'
        self.hook(m, sid='a/b')
        e = json.load(open(os.path.join(self.home, 'inbox', 'a_b.json')))
        self.assertEqual(e['prompt'], m)

def sc_chat_reply(test, run, msg):
    """chat_reply on a hand-made run folder, inside the test repository."""
    old = os.getcwd(); os.chdir(test.repo)
    try:
        sc.G.ROOT = test.repo; sc.G.a = sc.argparse.Namespace(fake=None, no_open=True, log=None, no_log=True, base='HEAD')
        return sc.chat_reply({'run': run}, msg)
    finally: os.chdir(old)

def sc_status(run):
    return json.load(open(os.path.join(run, 'status.json')))

def sc_alive(pid):
    if not pid: return False
    try: os.kill(int(pid), 0); return True
    except OSError: return False


@unittest.skipIf(sc is None, 'controller not importable')
class Helpers(unittest.TestCase):
    def test_number_criteria_edge_cases(self):
        n = lambda t: sc.number_criteria(t)
        self.assertEqual(n('Story\nAC1: shows 10\nAC 2 - returns 404\n')[1:], ('AC1: shows 10\nAC2: returns 404', False))
        self.assertEqual(n('AC1: a\nAC1: b\n')[1:], ('AC1: a\nAC2: b', False))
        self.assertEqual(n('- AC1: a\n- AC2: b\n')[1:], ('- AC1: a\n- AC2: b', True))          # consistent ACs: unchanged
        self.assertEqual(n('## Acceptance criteria\n| Case | Expected |\n|---|:--:|\n| unknown id | 404 |\n| a \\| b | c |\n')[1],
                         'AC1: Case: unknown id; Expected: 404\nAC2: Case: a | b; Expected: c')
        new, crit, _ = n('## Acceptance criteria\n- shows 10\n## Security requirements\n- encrypt at rest\n')
        self.assertEqual(crit, 'AC1: shows 10'); self.assertIn('## Security requirements\n- encrypt at rest', new)
        self.assertFalse(n('Background\n- the old page is slow\n- users complain\n')[2])
        self.assertTrue(n('- shows 10\n- 404\n')[2]); self.assertTrue(n('Story\nAC 1 - x\nAC 2 - y')[2])
        self.assertEqual(n('AC1: first\nAC2 - second\n')[1:], ('AC1: first\nAC2: second', False))
        self.assertEqual(n('AC 1 - first\nAC 1 - second\n')[1:], ('AC1: first\nAC2: second', False))
        self.assertEqual(n('Acceptance Criteria\n| Case | Expected |\n| unknown | |\n| known | 200 |\n')[1],
                         'AC1: Case: unknown; Expected: (empty)\nAC2: Case: known; Expected: 200')

    def test_number_criteria_loses_no_text(self):
        """Every word of every line of the paste is still in the numbered story (bullets, labels and table
        pipes aside)."""
        import re as _re
        samples = [JIRA, 'Acceptance Criteria\nThe user can:\nsee the list\nget a 404\n\nNotes\nx', 'Acceptance criteria:\n# a b\n# c d\n## e f',
                   '\u2022 shows 10\n\u2022 404 for unknown', '## Acceptance criteria\n1. shows 10\n2. 404\n   - empty body\n## Definition of Done\n- tests',
                   'Story\nAC 1 - shows 10\nAC-2: 404', 'Acceptance Criteria\nScenario: a\nGiven x\nWhen y\nThen z', 'Given x\nWhen y\nThen z\nGiven p\nThen q',
                   'Acceptance Criteria\nGiven\tWhen\tThen\nx\ty\tz', 'Acceptance Criteria\n||Case||Expected||\n|unknown id|404|',
                   'Story\nAC1: shows 10\nAC 2 - returns 404\n', 'Background\n- the old page is slow\n- users complain\n',
                   '## Acceptance criteria\n- shows 10\n## Security requirements\n- encrypt at rest\n']
        for t in samples:
            new = sc.number_criteria(t)[0]
            words = lambda x: set(_re.findall(r'[A-Za-z0-9]{2,}', x)) - {'AC', 'Acceptance', 'criteria', 'Criteria'}
            self.assertEqual(words(t) - words(new), set(), t)

    def test_verdict_counts_and_fences(self):
        self.assertEqual(sc.verdict_check('- **Verdict:** Approve — 2 of 3 criteria met; blockers: 0.')[0], None)
        self.assertEqual(sc.verdict_check('- **Verdict:** Approve — 3 of 3 criteria met; blockers: 0.')[0], 'Approve')
        self.assertEqual(sc.verdict_check('~~~\n- **Verdict:** Approve\n~~~\n- **Verdict:** Request changes — 1 of 2 criteria met; blockers: 1.')[0], 'Request changes')

    def test_notify_private(self):
        d = tempfile.mkdtemp(); rec = os.path.join(d, 'rec.py'); out = os.path.join(d, 'out')
        open(rec, 'w').write(f'import sys\nopen({out!r}, "a").write(sys.argv[1] + "\\n")\n')
        old = dict(os.environ)
        try:
            os.environ.update(STORY_CTL_NOTIFY='private', STORY_CTL_NOTIFY_CMD=f'"{sys.executable}" "{rec}"')
            sc.G.S = d; sc.notify('x')
            self.assertEqual(open(out).read(), 'Story\n')
        finally:
            os.environ.clear(); os.environ.update(old); shutil.rmtree(d)

    def test_activity_reads_a_growing_log(self):
        d = tempfile.mkdtemp(); p = os.path.join(d, 'events.jsonl')
        lines = IMPL_EVENTS.splitlines(True)
        open(p, 'w').write(''.join(lines[:6]) + lines[6][:10])            # the last line half written
        act = sc.Activity(p); act.read()
        self.assertEqual(len(act.acts), 2)
        open(p, 'w').write(IMPL_EVENTS); act.read()
        self.assertEqual([x[0] for x in act.acts], ['note', 'read', 'read', 'edit', 'run', 'run'])
        self.assertEqual(act.acts[4][2], '✗ exit 1'); self.assertEqual(act.acts[5][2], '✓')
        self.assertEqual(sc.model_desc(act), 'GPT-6.1 Sol, effort medium')
        open(p, 'a').write(events({'type': 'session.usage_record', 'data': {'usage': {'model': 'gpt-6-luna', 'modelDisplayName': 'GPT-6 Luna', 'reasoningEffort': 'high'}}}))
        act.read(); self.assertEqual(sc.model_desc(act), 'GPT-6.1 Sol → GPT-6 Luna, effort high')
        open(os.path.join(d, 'e2'), 'w').write(IMPL_EVENTS.splitlines(True)[0])
        e2 = sc.Activity(os.path.join(d, 'e2')); e2.read(); self.assertEqual(sc.model_desc(e2), 'claude-sonnet-5.5, default effort')
        self.assertEqual(sc.model_desc(sc.Activity(os.path.join(d, 'none'))), '')
        self.assertEqual(sc.Activity(os.path.join(d, 'none')).read(), [])
        shutil.rmtree(d)

    def test_number_criteria_formats(self):
        n = lambda t: sc.number_criteria(t)[1]
        self.assertEqual(n(JIRA), 'AC1: The list shows the last 10 transfers\n  - newest first\nAC2: An unknown account id returns 404')
        self.assertEqual(n('Acceptance Criteria\nThe user can:\nsee the list\nget a 404\n\nNotes\nx'), 'AC1: see the list\nAC2: get a 404')
        self.assertEqual(n('Acceptance criteria:\n# filter by date\n# filter by payee\n## case-insensitive'),
                         'AC1: filter by date\nAC2: filter by payee\n  - case-insensitive')
        self.assertEqual(n('\u2022 shows 10\n\u2022 404 for unknown'), 'AC1: shows 10\nAC2: 404 for unknown')
        self.assertEqual(n('## Acceptance criteria\n1. shows 10\n2. 404\n   - empty body\n## Definition of Done\n- tests'), 'AC1: shows 10\nAC2: 404\n  - empty body')
        self.assertEqual(n('Story\nAC 1 - shows 10\nAC-2: 404'), 'AC1: shows 10\nAC2: 404')
        self.assertEqual(n('Acceptance Criteria\nScenario: a\nGiven x\nWhen y\nThen z\nScenario: b\nGiven p\nThen q'),
                         'AC1: Scenario: a\n  Given x\n  When y\n  Then z\nAC2: Scenario: b\n  Given p\n  Then q')
        self.assertEqual(n('Given x\nWhen y\nThen z\nGiven p\nThen q'), 'AC1: Given x\n  When y\n  Then z\nAC2: Given p\n  Then q')
        self.assertEqual(n('Acceptance Criteria\nGiven\tWhen\tThen\nx\ty\tz'), 'AC1: Given: x; When: y; Then: z')
        self.assertEqual(n('Acceptance Criteria\n||Case||Expected||\n|unknown id|404|'), 'AC1: Case: unknown id; Expected: 404')
        self.assertEqual(n('Acceptance criteria: shows 10'), 'AC1: shows 10')
        self.assertEqual(n(STORY), 'AC1: the list shows the thing\nAC2: an unknown id returns 404')
        for t in ('go', 'how do I use this?', ''):
            self.assertEqual(n(t), '')
        self.assertTrue(sc.number_criteria(JIRA)[2]); self.assertFalse(sc.number_criteria('shows 10\n404 for unknown')[2])

    def test_parse_criteria(self):
        s = '- AC1: first\n  continued\n- AC2: second\n- a note bullet\n\n**AC3:** bold\nAC4: plain\n- sub bullet\nAfter\n'
        self.assertEqual(sc.parse_criteria(s), '- AC1: first\n  continued\n- AC2: second\n**AC3:** bold\nAC4: plain\n- sub bullet')

    def test_verdicts(self):
        self.assertEqual(sc.verdict_check(APPROVE), ('Approve', ''))
        self.assertEqual(sc.verdict(RC_Q), 'Request changes')
        self.assertEqual(sc.verdict(NEEDS), 'Needs answers')
        self.assertIsNone(sc.verdict('- **Verdict:** Approved'))
        fenced = '```\n- **Verdict:** Approve\n```\n' + RC
        self.assertEqual(sc.verdict(fenced), 'Request changes')
        self.assertIsNone(sc.verdict('> quoted **Verdict:** Approve'))

    def test_same_edits(self):
        n = lambda r: sc.edit_norm(r)
        self.assertTrue(sc.same_edits(n(RC), n(RC_SAME)))
        self.assertFalse(sc.same_edits(n(RC), n(RC_NUM)))
        self.assertFalse(sc.same_edits(None, n(RC)))

    def test_impl_incomplete(self):
        self.assertEqual(sc.impl_incomplete(IMPL), '')
        self.assertIn('code 2', sc.impl_incomplete(IMPL, 2))
        self.assertTrue(sc.impl_incomplete('no header'))

if __name__ == '__main__':
    unittest.main(verbosity=1)
