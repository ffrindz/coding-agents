"""The story workflow: investigate, the handoff stop, up to 3 implement and review rounds."""
import argparse, difflib, hashlib, json, os, re, shlex, shutil, signal, subprocess, sys, time, uuid
from .state import *
from .criteria import *
from .reports import *
from .repo import *
from .journal import *
from .activity import *
from .progress import *
from .notify import *
from .agents import *

def workflow(story, criteria, base_desc, INV, IMP, REV):
    # 1. investigate, then stop for the developer
    answers_q = []
    handoff, _ = run_agent(INV, 'Story mode\n\n' + story, 'investigate', True)
    G.cur['handoff'] = handoff
    while True:
        hq = field(handoff, 'Questions for the analyst')
        r = ask(handoff + '\n\nReply go to implement, answer the questions above (implement and review will use your answers), '
                'start your reply with "Change the plan:" to have investigate plan again, or reply stop.', 1 if not G.answers else len(G.answers) + 1, 'the handoff', hq)
        while is_question(r) and not re.match(r'\s*change the plan\b', r, re.I):
            handoff_note = answer_question(r, handoff, 'plan')
            answers_q.append((r, handoff_note))
            r = ask('Answer to your question:\n\n' + handoff_note + '\n\n---\n\n' + handoff + '\n\nReply go to implement, answer the questions above, start your reply with "Change the plan:", or reply stop.', f'h{len(answers_q)}', 'the handoff', hq)
        if re.match(r'\s*change the plan\b', r, re.I):
            G.answers.append(('Plan change at the handoff', r))     # kept for every later task, and for later replans
            handoff, _ = run_agent(INV, f'Story mode\n\n{story}\n\nPrevious handoff:\n{handoff}\n\nChanges and answers from the user so far '
                                   f'(verbatim, oldest first; all of them still apply unless a later one replaces it):\n'
                                   + answers_block().split(':\n', 1)[1], 'reinvestigate', True)
            G.cur['handoff'] = handoff
            continue
        if not is_go(r):
            G.answers.append(('At the handoff', r))
        break

    # 2. rounds
    rev_task = f'Review the changes on the current branch against base {base_desc}\n\nAcceptance criteria (from the story, verbatim):\n{criteria}\n\nImplement report:\n'
    imp_prompt = 'Round 1 of 3\nImplement the task based on this handoff.\n\n' + handoff + answers_block()
    prev_edit = None
    for n in (1, 2, 3):
        impl, code = run_agent(IMP, imp_prompt, f'implement-r{n}', False)
        G.cur['impl'] = impl
        why = impl_incomplete(impl, code)
        if why:
            r = ask(f'Implement could not finish round {n} ({why}). Its report:\n\n{impl}\n\nReply "review" to send the work to review anyway, "stop" to stop here, or tell implement what to do next.', f'impl{n}', f'round {n} after implement could not finish ({why})')
            if not re.fullmatch(r'\s*review\.?\s*', r, re.I):
                G.answers.append((f'During round {n} (to implement)', r))
                impl, code = run_agent(IMP, f'Round {n} of 3\nContinue this round. Your previous report is below; the developer\'s instruction is the last answer.\n\n{impl}{answers_block()}', f'implement-r{n}b', False)
                G.cur['impl'] = impl
                why2 = impl_incomplete(impl, code)
                if why2:
                    stop_with(f'Stopped — implement could not finish round {n} twice ({why2})')
        inv = field(handoff, 'Behavioural invariants')
        inv_block = '' if (not inv or re.match(r'^(none|n/a)\b', inv, re.I)) else '\n\nBehavioural invariants from the handoff (verbatim):\n' + inv
        rev, _ = run_agent(REV, rev_task + impl + answers_block() + inv_block, f'review-r{n}', True)
        G.cur['rev'] = rev
        v, bad = verdict_check(rev, sum(1 for l in criteria.splitlines() if AC_RE.match(l)))
        G.cur['verdicts'].append(verdict_line(rev).replace('- **Verdict:** ', '') if v else f'invalid ({bad})')
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
            rq = field(rev, 'Questions for the author or analyst')
            shown = verdict_line(rev) + '\n- **Questions for the author or analyst:** ' + rq
            if v == 'Request changes':
                shown += '\n- **Edit:** ' + field(rev, 'Edit')
            tail = f'\n\nRound {n} of 3. Reply with your answers and I will continue, or reply stop.'
            r = ask(shown + tail, len(G.answers) + 2, f'the round {n} review questions', rq)
            while is_question(r):
                note = answer_question(r, rev, 'review')
                r = ask('Answer to your question:\n\n' + note + '\n\n---\n\n' + shown + tail, f'q{n}-{len(answers_q)}', f'the round {n} review questions', rq)
                answers_q.append((r, note))
            if not is_go(r):
                G.answers.append((f'After round {n} review', r))
            imp_prompt = f'Round {n+1} of 3\nFix the items under Edit in this review, if any, and apply the user\'s answers to its questions.\n\n{rev}{answers_block()}'
        else:
            imp_prompt = f'Round {n+1} of 3\nFix the items under Edit in this review.\n\n{rev}{answers_block()}'

__all__ = [n for n in dir() if not n.startswith('__')]
