# Coding agents for Angular + Java

Six GitHub Copilot custom agents for working on Angular + Java codebases (Maven or Gradle, Spring or plain Java). Each one is a single `.agent.md` file.

| Agent | Use it to | Edits code? | Model |
|---|---|---|---|
| **investigate** | Find the root cause of a defect or plan a change. Produces a handoff that implement can act on without searching again. | No (terminal for git and one test run) | GPT-6 Luna |
| **implement** | Make the change: smallest complete edit, then compile, test, lint and run the full suite. | Yes | GPT-6.1 Sol (falls back to GPT-6 Luna) |
| **review** | Check the branch against the story's acceptance criteria, one verdict per criterion with code and test evidence. | No (terminal for git and one test run) | GPT-6.1 Sol (falls back to GPT-6 Luna) |
| **explain** | Answer "how does X work?" in plain language, with cited code and a diagram when useful. | No (read and search, plus read-only GitHub) | GPT-6 Luna |
| **story** | Run a whole story from the agent window: paste it, reply at each stop, get the final report. It only relays to the `story-ctl` workflow controller, which runs investigate, implement and review (up to 3 rounds) and does all the bookkeeping in code. | No (runs only the controller command) | GPT-6 Luna for the relay; each agent uses its own model |
| **spec** | Turn a UI team's requirement into a spec for a systems analyst: functional and non-functional requirements, our API's contract, which downstream endpoints supply each field, and the field mapping. | No (read, search and GitHub only) | GPT-6 Luna |

## Prerequisites

**Copilot**
- VS Code with the GitHub Copilot extension, signed in, with custom agents available in Copilot chat. The Copilot CLI (`copilot`) works too, except for handoff buttons.
- **For story:** Python 3 (`python3` on macOS and Linux, `py` on Windows), the Copilot CLI installed and signed in (the controller runs the agents through it), and VS Code's `code` command on your PATH so each stop opens in an editor tab by itself (Command Palette → "Shell Command: Install 'code' command in PATH" on macOS; the Windows installer adds it).
- A Copilot plan whose model list includes **GPT-6 Luna** (all agents) and **GPT-6.1 Sol** (implement and review). Without Sol, implement and review fall back to Luna, which is fine for normal stories but weaker on refactors (see [Models](#models)).
- **For spec only:** the GitHub MCP server added to VS Code and signed in with an account that can read your organisation's repositories, with these three read-only tools enabled in the tool picker: `get_file_contents`, `search_code`, `search_repositories`. Without it, spec can still map from attached Swagger files but can't check them against the repositories.

**The project you run the agents in**
- Open the project's root folder as the workspace. It must be a git repository with a base branch (`origin/main`, `main`, `master` or `develop`); review compares the branch against it.
- The project's build must run locally from the VS Code terminal: a JDK matching the project, the Maven or Gradle wrapper (`./mvnw`, `./gradlew`), and Node matching the project's `engines` or `.nvmrc`, or the project's own Node wrapper. implement and review compile and run tests; if the build can't run, they can't prove anything works.
- Dependencies already downloaded (one successful build beforehand). investigate and review never run `npm install`, `mvn install` or clean builds, so without the downloads their test runs fail.
- Tests that need Docker or other infrastructure are skipped and reported as not run, so expect "Met, untested" verdicts for anything only those tests cover.

**Terminal approvals**
- Only implement can edit files. explain and spec have no terminal and no edit tool, so they cannot change anything. investigate and review keep a terminal for git and one targeted test run; their prompts allow only read-only commands, and they report any file that changed during their run. implement, investigate and review run terminal commands (git, builds, tests). VS Code asks you to approve each command unless you allow it in its terminal auto-approve settings. Allowing read-only commands (`git status`, `git diff`, `git log`) and the project's build and test commands saves most prompts; keep anything that changes or deletes files or branches on manual approval.

**Optional**
- [ripgrep](https://github.com/BurntSushi/ripgrep) (`rg`) on your PATH: `brew install ripgrep` (macOS) or `winget install BurntSushi.ripgrep.MSVC` (Windows). Copilot's built-in search tools and the Copilot CLI bring their own copy, but the model sometimes types `rg` in the VS Code terminal; without it that call fails and the agent falls back to slower searches.
- The prompts mention a `/ponytail` skill. The agents were tested without it and work the same.

## Setup

**Quickest:** download the repository, then from its root run `sh tools/install.sh` (macOS, Linux) or `powershell -ExecutionPolicy Bypass -File tools\install.ps1` (Windows). It copies the agents to your personal agents folder and the workflow controller to `~/.story-ctl/`, and checks that Python, the Copilot CLI and the `code` command are there. Or by hand:

1. Download the six `.agent.md` files from this repository (or the whole repository as a ZIP).
2. Copy them into the `agents` folder inside your user-level `.copilot` folder, which VS Code and the Copilot CLI both read personal agents from: `~/.copilot/agents` on macOS and Linux, `%USERPROFILE%\.copilot\agents` on Windows. Create the folder if it doesn't exist. Copy only the `.agent.md` files: Copilot tries to load every `.md` file in that folder as an agent.
3. Restart VS Code (or open a new Copilot chat) and check that the six agents appear in the agent picker.
4. For story, also copy `tools/story-ctl.py` to `~/.story-ctl/story-ctl.py` (`%USERPROFILE%\.story-ctl\story-ctl.py` on Windows).

**Updating:** download the new `.agent.md` files and replace the old ones. Each report's first line shows the agent version (for example `# Review (review v3.23)`), so you can tell which version produced it; [CHANGELOG.md](CHANGELOG.md) lists what changed.

## Workflow

### Coding a story: investigate → implement → review

1. **investigate:** paste the story or defect (with its acceptance criteria). It replies with a handoff.
2. **implement:** in a new chat, paste the handoff (or the story itself for a small change). It edits, runs the checks and reports.
3. **review:** in a new chat, paste the implement report (or the acceptance criteria). It replies Approve, Request changes or Needs answers.
4. On Request changes, paste the review into a new implement chat ("Fix the items under Edit in this review"), then review again. Most stories finish in 1–2 rounds.
5. On Needs answers, add your answers under the review before pasting it into implement. Don't send it without them: implement can't settle the question either.

Every implement report starts with the story's acceptance criteria, so the report you paste into review always carries them.

Use a fresh chat for each step so each agent starts from the report alone. The handoff buttons at the end of a reply do this for you in VS Code's Local harness; the Copilot harness (VS Code's default from 2026, and the Copilot CLI) ignores them, and VS Code removes the Local harness by February 2027. The story agent below replaces them.

### Run a whole story in the agent window (recommended)

1. Open the repository you are changing, start a new chat, pick **story** in the agent dropdown and paste the story as you copied it from Jira. The controller numbers the acceptance criteria `AC1:`, `AC2:`, ... itself, without a model, and shows them in its first reply. It reads criteria under an "Acceptance criteria" heading as bullets, numbers, Jira markup, Given/When/Then, a table, or one per line. If you paste only the criteria with no heading or bullets, it shows how it numbered them and waits for `go`. If the numbering is wrong, reply `stop` and paste again. Criteria already written as `AC1:` lines are used as they are.
2. Within seconds it replies "Started". The first time, VS Code asks you to approve the `story-ctl.py --chat` command; allow it for good (for that exact command) so later messages run without a prompt.
3. A live view, `progress.md`, opens in an editor tab, and the reply links it too. Press Cmd+Shift+V (Ctrl+Shift+V) for the preview; it updates by itself as the run goes. It shows:
   - the criteria;
   - each step with the model and reasoning effort it ran on (both models if it switched to its fallback), its time, credits, report and a full list of what it did;
   - your replies;
   - the files changed in the working tree;
   - what the running agent is doing now: the files it reads, its searches, the commands it runs (with ✓ or ✗) and the files it edits.

   `status` in the chat shows the last five of these actions.
4. When the plan is ready it opens in an editor tab; press Cmd+Shift+V (Ctrl+Shift+V) for the preview, where code links open the files. Reply in the same chat:

   | At | Reply | What happens |
   |---|---|---|
   | the handoff | `go`, your answers, or `Change the plan: ...` | implement starts with your answers, or investigate plans again |
   | any stop | a question ending with `?` | the explain agent answers it, the stop opens again with the answer, and you stay at the stop |
   | a review with questions | your answers | the next round fixes the Edit items and applies your answers |
   | implement could not finish a round | `review`, or an instruction for implement | the work goes to review, or implement continues with your instruction |
   | any stop | `stop` | the run ends there and can be resumed later |
   | any time | `status` | what is running, on which model and effort, its last five actions, credits and minutes so far, or where it is waiting |
   | after a stop or an interruption | `resume` | continues from the last finished step (in a new chat too: it picks up the last run in this repository) |
   | any time | `stats`, `report`, `help` | your run statistics, a problem report for this run, the list of replies |

5. implement and review run in the background, at most 3 rounds; a review with questions opens in a tab and waits for your reply. The run keeps going if you close the chat or switch to other work; when it ends, `status` shows the result and links the final report (open items, every handoff question with the assumption used, your answers, the last implement report and the last review).
6. The chat cannot tell you by itself when something is ready, because it only replies to your messages. So at each stop and when the run ends, the controller shows a desktop notification, for example "Story: BANK-77 — The plan is ready (in an editor tab). Reply in the chat". At the end it also opens the final report in a tab. On macOS it uses `osascript`. On Windows it uses a PowerShell toast notification, and on Linux `notify-send`; both of these are untested so far. A system that blocks notifications skips them without affecting the run. Set `STORY_CTL_NOTIFY=0` to turn them off. On macOS the notifications come from "Script Editor": if none appear, allow it in System Settings → Notifications.
7. A new chat is a new story. Only one story at a time runs in a working tree, because two would mix their changes and reviews. If another one is working or waiting, you're told so, and you can reply `set aside` to end the waiting one (it stays resumable) and start the new one. For two stories at once, use a second worktree (`git worktree add`). If you switch branch or commit while a run is waiting for you, your reply isn't used: the stop opens again so you can check, and you reply once more. `STORY_CTL_NOTIFY=private` leaves the story title out of notifications. A message sent while an agent is still working is not used (it says so); send it again when the stop opens.

How it stays reliable: the story agent never orchestrates. For every message it runs one fixed command and shows its output; your message reaches the controller through the agent's own hook (exactly as typed, never retyped by the model), and a second hook denies every other command, so the relay cannot edit files or do the work itself. Handoffs, answers, verdicts and rounds are the workflow controller's code described below. Each chat turn costs the relay about 0.1 credit; the agents cost what they cost from the terminal. If your organisation turns hooks off, the story agent replies that it could not read your message: run the story from the terminal instead.

### Run a whole story from the terminal

`tools/story-ctl.py` is the workflow controller the story agent relays to; you can also run it directly in VS Code's terminal. The bookkeeping is code instead of a model: handoffs and reports are passed on byte for byte, all your answers go to every later task, verdicts and rounds are read and counted exactly, and it always stops after the handoff and when a review has questions. In tests it ran 30 stories with no orchestration mistake (a model-run story agent made one in about 1 run in 6). It needs Python 3 and the Copilot CLI.

1. Install it as in [Setup](#setup) (or copy `tools/story-ctl.py` anywhere).
2. Save the story in a text file, as copied from Jira. The criteria are numbered `AC1:`, `AC2:`, ... as in the agent window, and the numbered list is printed at the start. You can also write them as `AC1:` lines yourself; a criterion can continue on indented or bulleted lines below it.
3. In VS Code's terminal, at the root of the repository you are changing: `python3 <path>/story-ctl.py story.txt --base main`
4. At each stop it prints the agent's text and opens it in VS Code; press Cmd+Shift+V (Ctrl+Shift+V) for the Markdown preview, where code links open the files. Type your reply and finish with an empty line:

   | At | Reply | What happens |
   |---|---|---|
   | the handoff | `go`, your answers, or `Change the plan: ...` | as with the story agent |
   | any stop | a question ending with `?` | the explain agent answers it and you stay at the stop |
   | a review with questions | your answers | the next round fixes the Edit items and applies your answers |
   | implement could not finish a round | `review`, or an instruction for implement | implement continues with your instruction, or the work goes to review |
   | any stop | `stop`, or Ctrl-D with nothing typed | the run ends there; nothing more is changed |

5. It stops early when a review asks for the same changes as the round before, and after round 3. It also stops, with a message and without reverting anything, when a review report has no valid verdict, when investigate, review or explain changed your working tree (they run with file edits and `git commit`/`push`/`reset`/`checkout` denied, but a shell command could still write, so the controller compares the tree before and after), or when implement failed after changing files (it is not retried over partial edits). `--base` is fixed to a commit when the run starts. The final report lists the open items, every handoff question with the assumption used, and all your answers, then the last implement report and the last review in full.

Everything is saved under `.git/story-ctl/<time>/` (in a linked worktree, under that worktree's git folder; outside the working tree, never committed), so you can reopen any step. `python3 tools/test_story_ctl.py` runs the controller's own tests offline (no Copilot, no credits).

Three commands help with real use:
- **Continue a run later:** `python3 <path>/story-ctl.py --resume last` (or the run's folder) continues a run you stopped at a stop (`stop`, for example to think about a question), a run that was interrupted (Ctrl-C, a closed terminal, a sleeping laptop), or one whose agent step failed twice (a service outage). Finished steps and your replies are replayed from the run's journal without calling the agents or spending credits again; an implement step that was interrupted mid-way asks before it runs again over its partial edits.
- **See how it is going:** every run adds one line to `~/.story-ctl/runs.jsonl` on your machine (nothing is sent anywhere; `--no-log` to skip). `python3 <path>/story-ctl.py --stats` shows outcomes, how many runs were approved and in how many rounds, credits per agent and minutes per run. Credits and time so far are also shown at each stop and in the final report.
- **Report a problem:** `python3 <path>/story-ctl.py --report last` writes `problem-report.md` in the run's folder with the agent versions, the criteria, each stop's questions and your reply, and each round's verdict line. Replace names with neutral ones, fill in the last three lines, and share it so the problem becomes a test.

### Add a repository instructions file

Copilot reads `.github/copilot-instructions.md` in each repository automatically, in every agent and in plain chat. In tests it cut implement and review rounds from 7 to 5 over 4 stories and credits by about 13%, with the same or better quality. Keep it to about 40 lines, and have your documentation agent maintain it:
- build, test and lint commands with the flags the project needs (for example `-Dskip.installnodenpm -Dskip.npm`), including one test class and one integration or Spock spec;
- the test levels and where each lives (unit, integration, functional Spock specs and their naming);
- layout and conventions: where resources, services, entities, migrations, security and UI screens live, error handling and how errors map to HTTP status;
- traps the agents should know (test teardown that a new constraint breaks, test transactions that hide database-generated values);
- that Markdown files are generated by the documentation agent.

### Answering questions

Each question shows the options, **If unanswered:** what will be built if you don't answer (existing behavior always stays as today unless a criterion asks for a change), and **Recommendation:** what the agent advises, which is used only if you agree. To take a recommendation, name the question (for example "missing version: use your recommendation"), or answer in your own words.

### Writing a spec

spec stands alone; it is not part of the coding chain. Run it in the workspace of the API your team owns, and give it:

- the UI team's requirement;
- each downstream API's Swagger/OpenAPI file attached (JSON or YAML);
- each downstream API's application ID or repository name, so it can check the attached file against the repository.

It writes a plain-English part for the analyst (functional and non-functional requirements, and a field mapping table) and a technical part for engineers. When the attached file and the repository differ, it lists the differences and asks which one is deployed. Non-functional requirements the requirement and the downstream specs don't state (response times, caching, auth, audit) come back as questions, never as invented targets.

**When the requirement changes or you have answers:** in a new chat, give the same requirement and downstream APIs, then "Previous spec:" with the whole previous spec, then your changes or answers. spec keeps the parts the changes don't affect word for word, adds "What changed since the previous spec" at the top, and costs about a third to a half of a fresh spec.

### Asking how something works

Ask explain a question in a chat. It does not change anything.

It can also read other teams' code: give the repository as a local path, or name the application by app ID, repository or name and it reads it from GitHub (read-only GitHub tools). Each answer says who can do the thing (roles and guards in the API and UI) and which tables, services and settings it touches. If what you ask about does not exist, it says so and lists the closest matches. When a document disagrees with the code, it answers from the code and names the document.

## Models

Chosen by measured quality first, then credits and time per correct result (headless Copilot CLI runs on public sample apps, November 2026):

- **implement + review on Sol:** on normal stories Sol and Luna both delivered correct code (4 of 4 each). On a refactor that had to keep the API unchanged, Luna's refactors changed API responses in 5 of 5 runs; Sol's kept every response except an item-order change in 3 of 7. Luna review approved broken refactors 6 of 11 times, Sol review 1 of 6. Sol costs about 15–25× more credits per step.
- **investigate, explain, spec on Luna:** Sol and Gemini 4 Neon scored no better on these. Neon's spec writing was judged worse (longer, wordier) at about 20× the credits.
- **Effort:** default. High or extra-high effort never scored higher.

## Known limits

- **Response order in refactors:** no model caught a refactor that changed the order of items in an API response. For refactors that must keep the API unchanged, compare recorded responses before and after (a contract or snapshot test).
- **Code quality:** prompt rules did not make review find duplication or null-handling bugs. Use build tools (Checkstyle, PMD, SpotBugs, ESLint, ArchUnit): implement runs the full build and review will not approve without it, so build rules are enforced automatically.
- **story retypes reports:** the orchestrator passes each report on by rewriting it, so 1 or 2 lines per story can differ by a word or a character from what the subagent wrote. The acceptance criteria reached review verbatim in every test. If a line in a handoff or review looks wrong, check the file it cites.
- **Prompt rules are not guarantees:** a rule such as "never run git checkout" was followed 1 time in 4. Block commands with tool settings (for example terminal auto-approve deny lists in VS Code) instead.

## Changing an agent

1. Bump the version in the agent's report header (for example `review v3.23` → `v3.24`).
2. Test before and after on the same cases, and keep the change only if the after runs are better and nothing else got worse.
3. Add an entry to [CHANGELOG.md](CHANGELOG.md) with what changed and the result, including changes you tried and dropped.

Check the "Tried and dropped" list in the changelog before adding a rule: several obvious-looking rules were tested and did not work.
