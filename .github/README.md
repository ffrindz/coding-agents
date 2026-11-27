# Coding agents for Angular + Java

Six GitHub Copilot custom agents for working on Angular + Java codebases (Maven or Gradle, Spring or plain Java). Each one is a single `.agent.md` file.

| Agent | Use it to | Edits code? | Model |
|---|---|---|---|
| **investigate** | Find the root cause of a defect or plan a change. Produces a handoff that implement can act on without searching again. | No (terminal for git and one test run) | GPT-6 Luna |
| **implement** | Make the change: smallest complete edit, then compile, test, lint and run the full suite. | Yes | GPT-6.1 Sol (falls back to GPT-6 Luna) |
| **review** | Check the branch against the story's acceptance criteria, one verdict per criterion with code and test evidence. | No (terminal for git and one test run) | GPT-6.1 Sol (falls back to GPT-6 Luna) |
| **explain** | Answer "how does X work?" in plain language, with cited code and a diagram when useful. | No (read and search, plus read-only GitHub) | GPT-6 Luna |
| **story** (trial) | Run a whole story: investigate, implement and review as subagents, passing the acceptance criteria and your answers to every round, stopping whenever a review has questions, and repeating fix rounds until review approves or 3 rounds pass. | No (only starts the other agents) | GPT-6 Luna; each subagent uses its own model |
| **spec** | Turn a UI team's requirement into a spec for a systems analyst: functional and non-functional requirements, our API's contract, which downstream endpoints supply each field, and the field mapping. | No (read, search and GitHub only) | GPT-6 Luna |

## Prerequisites

**Copilot**
- VS Code with the GitHub Copilot extension, signed in, with custom agents available in Copilot chat. The Copilot CLI (`copilot`) works too, except for handoff buttons.
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

1. Download the six `.agent.md` files from this repository (or the whole repository as a ZIP).
2. Copy them into the `agents` folder inside your user-level `.copilot` folder, which VS Code and the Copilot CLI both read personal agents from: `~/.copilot/agents` on macOS and Linux, `%USERPROFILE%\.copilot\agents` on Windows. Create the folder if it doesn't exist. Copy only the `.agent.md` files: Copilot tries to load every `.md` file in that folder as an agent.
3. Restart VS Code (or open a new Copilot chat) and check that the six agents appear in the agent picker.

**Updating:** download the new `.agent.md` files and replace the old ones. Each report's first line shows the agent version (for example `# Review (review v3.23)`), so you can tell which version produced it; [CHANGELOG.md](CHANGELOG.md) lists what changed.

## Workflow

### Coding a story: investigate → implement → review

1. **investigate:** paste the story or defect (with its acceptance criteria). It replies with a handoff.
2. **implement:** in a new chat, paste the handoff (or the story itself for a small change). It edits, runs the checks and reports.
3. **review:** in a new chat, paste the implement report (or the acceptance criteria). It replies Approve, Request changes or Needs answers.
4. On Request changes, paste the review into a new implement chat ("Fix the items under Edit in this review"), then review again. Most stories finish in 1–2 rounds.
5. On Needs answers, add your answers under the review before pasting it into implement. Don't send it without them: implement can't settle the question either.

Every implement report starts with the story's acceptance criteria, so the report you paste into review always carries them.

Use a fresh chat for each step so each agent starts from the report alone. The handoff buttons at the end of a reply do this for you in VS Code (the Copilot CLI ignores them).

### Run a whole story with the workflow controller (recommended)

`tools/story-ctl.py` runs investigate, implement and review for you, with you in the loop at the same points as the story agent, but the bookkeeping is code instead of a model: handoffs and reports are passed on byte for byte, all your answers go to every later task, verdicts and rounds are read and counted exactly, and it always stops after the handoff and when a review has questions. In tests it ran 30 stories with no orchestration mistake (the story agent made one in about 1 run in 6). It needs Python 3 and the Copilot CLI.

1. Copy `tools/story-ctl.py` anywhere (for example next to your agent files).
2. Save the story with its acceptance criteria (lines starting `AC1:`, `AC2:`, ...) in a text file.
3. In VS Code's terminal, at the root of the repository you are changing: `python3 <path>/story-ctl.py story.txt --base main`
4. At each stop it prints the agent's text and opens it in VS Code; press Cmd+Shift+V (Ctrl+Shift+V) for the Markdown preview, where code links open the files. Type your reply and finish with an empty line:

   | At | Reply | What happens |
   |---|---|---|
   | the handoff | `go`, your answers, or `Change the plan: ...` | as with the story agent |
   | any stop | a question ending with `?` | the explain agent answers it and you stay at the stop |
   | a review with questions | your answers | the next round fixes the Edit items and applies your answers |
   | implement could not finish a round | `review`, `stop`, or an instruction for implement | implement continues with your instruction, or the work goes to review, or it stops |

5. It stops early when a review asks for the same changes as the round before, and after round 3. The final report lists the open items, every handoff question with the assumption used, and all your answers, then the last implement report and the last review in full.

Everything is saved under `.git/story-ctl/<time>/` (outside the working tree, never committed), so you can reopen any step.

### Add a repository instructions file

Copilot reads `.github/copilot-instructions.md` in each repository automatically, in every agent and in plain chat. In tests it cut implement and review rounds from 7 to 5 over 4 stories and credits by about 13%, with the same or better quality. Keep it to about 40 lines, and have your documentation agent maintain it:
- build, test and lint commands with the flags the project needs (for example `-Dskip.installnodenpm -Dskip.npm`), including one test class and one integration or Spock spec;
- the test levels and where each lives (unit, integration, functional Spock specs and their naming);
- layout and conventions: where resources, services, entities, migrations, security and UI screens live, error handling and how errors map to HTTP status;
- traps the agents should know (test teardown that a new constraint breaks, test transactions that hide database-generated values);
- that Markdown files are generated by the documentation agent.

### Or let the story agent run the chain (trial)

1. In a new chat, pick **story**, set the effort to **High**, and paste the story with its acceptance criteria. If you already know the answer to an open question, put it in this message.
2. It runs investigate, shows you the whole handoff and waits. Reply in one of three ways:

   | You want to | Reply | What happens |
   |---|---|---|
   | Proceed | `go` | implement starts |
   | Answer the handoff's questions | your answers, with or without `go` | implement starts; your answers go to implement and to every review |
   | Change the approach | `Change the plan: ...` | investigate plans again from the previous handoff and waits again |

3. implement and review then run by themselves, at most 3 rounds, each in a subagent with fresh context and its own model. Every implement and review gets your criteria and all your answers so far, exactly as you gave them.
4. If a review has questions (Needs answers, or Request changes with questions), it stops and shows the verdict, the questions and, for Request changes, the Edit items. Answer in the same chat and it carries on; the next round fixes the Edit items and applies your answers together. You only need to answer the new questions: earlier answers are passed on with them.
5. The final reply gives the result, each round's verdict, the criteria lines and open items, then the last implement report and the last review in full. If it ends "Not approved after 3 rounds", paste the "Last review" into a new implement chat and continue by hand.

Each question shows the options, **If unanswered:** what will be built if you don't answer (existing behavior always stays as today unless a criterion asks for a change), and **Recommendation:** what the agent advises, which is used only if you agree. To take a recommendation, name the question (for example "missing version: use your recommendation"), or answer in your own words.

Notes:
- Answering at the handoff is cheaper than answering later: in one test it saved a whole implement + review round (2 rounds instead of 3).
- Include the words "without stopping" in your first message to skip the handoff stop.
- Each subagent uses the model in its own agent file, but the effort you pick for story applies to all of them. Pick High, so implement and review run Sol at the effort they were tested with.
- Use the manual chain instead when you want to read or edit each step, for example on refactors.

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
