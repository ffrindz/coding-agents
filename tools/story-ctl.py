#!/usr/bin/env python3
"""story-ctl: run a whole story through investigate, implement and review, with you in the loop.

A small, deterministic workflow controller for the Copilot CLI. The three agents do the work; this script does
the bookkeeping that a model does unreliably: it passes every handoff and report on byte for byte, keeps all
your answers and gives them to every later task, reads the review verdict, counts rounds (at most 3), and stops
for you after the handoff and whenever a review has questions.

usage (from the root of the repository you are changing, for example in VS Code's terminal):
    python3 <path>/story-ctl.py story.txt [--base main]

story.txt holds the story with its acceptance criteria as lines starting "AC1:", "AC2:", ... (a criterion may
continue on indented or bulleted lines below it), or the story as copied from Jira: criteria under an
"Acceptance criteria" heading (bullets, numbers, Jira markup, Given/When/Then, a table, or one per line) are
numbered AC1:, AC2:, ... for you, without a model.
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
Live view: progress.md in the run folder opens in an editor tab at the start and is rewritten as the run goes: the
criteria, each step with its time, credits and report, your replies, and what the running agent is reading,
searching, running (with pass or fail) and editing, from its Copilot session log; `status` in the chat shows the
last few actions. Each finished step's actions are kept in <step>.activity.md.
Notifications: at each stop and when the run ends, a desktop notification (macOS osascript, Windows PowerShell
toast, Linux notify-send; STORY_CTL_NOTIFY=0 turns them off), and the final report opens in an editor tab.
Run log: each run adds one line to ~/.story-ctl/runs.jsonl (on your machine only; --log FILE, $STORY_CTL_LOG,
or --no-log); --stats summarises it (outcomes, rounds, credits per agent, minutes). Credits and time so far are
shown at every stop and in the final report.
--resume RUN (a run folder, or "last") continues a run that was interrupted (Ctrl-C, a closed terminal), that you
stopped at a stop, or whose agent step failed twice (a service outage): finished steps and your replies are
replayed from the run's journal without calling the agents again; an interrupted implement step asks before it
runs again over its partial edits.
--report RUN writes problem-report.md for a run (criteria, each stop's questions and your reply, each round's
verdict line), ready to neutralise and share when an agent does something wrong.
Test options: --fake <folder> plays back canned agent replies (<agent>-<k>.md; optional <agent>-<k>.sh runs
first in the repository, <agent>-<k>.exit the exit code, <agent>-<k>.credits the credits, <agent>-<k>.events a
Copilot session log for the progress page); --reply-cmd answers stops from a command.
Tests: python3 tools/test_story_ctl.py (no Copilot, no credits).
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))     # the storyctl package sits next to this file
from storyctl.cli import *                                          # every name, for the tests and for imports

if __name__ == '__main__':
    main()
