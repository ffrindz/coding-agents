#!/usr/bin/env python3
"""Offline tests for story-ctl.py: no Copilot, no credits. Run: python3 tools/test_story_ctl.py

Each workflow test makes a throwaway git repository, plays back canned agent reports (--fake) and answers the
stops from a queue (--reply-cmd), then checks the exact agent call sequence, the prompts and the final result.
"""
import importlib.util, json, os, shutil, subprocess, sys, tempfile, textwrap, unittest, warnings

HERE = os.path.dirname(os.path.abspath(__file__))
CTL = os.path.join(HERE, 'story-ctl.py')
spec = importlib.util.spec_from_file_location('story_ctl', CTL)
sc = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(sc)
except SystemExit:      # a controller that runs on import (before v1.1)
    sc = None

STORY = 'Story: add a thing\n\nAC1: the list shows the thing\nAC2: an unknown id returns 404\n'
HANDOFF = textwrap.dedent('''\
    # Handoff (investigate)
    - **Summary:** story — add a thing
    - **Questions for the analyst (story only):** Q1 should the thing be sorted? — **If unanswered:** keep today's order
    - **Plan (story only):** 1. add it''')
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
        self.env = dict(os.environ, STORY_CTL_LOG=self.log)

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

@unittest.skipIf(sc is None, 'controller not importable')
class Helpers(unittest.TestCase):
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
