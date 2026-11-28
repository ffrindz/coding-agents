#!/bin/sh
# Install the agents and the story-ctl controller for VS Code and the Copilot CLI (macOS, Linux).
# usage: sh tools/install.sh        (from the root of this repository)
set -e
cd "$(dirname "$0")/.."
AG="$HOME/.copilot/agents"; SC="$HOME/.story-ctl"
mkdir -p "$AG" "$SC"
if [ "$(cd "$AG" && pwd -P)" != "$(pwd -P)" ]; then
  cp ./*.agent.md "$AG/"
  echo "agents copied to $AG"
else
  echo "agents: $AG already points at this repository"
fi
cp tools/story-ctl.py "$SC/story-ctl.py"
rm -rf "$SC/storyctl" && mkdir -p "$SC/storyctl" && cp tools/storyctl/*.py "$SC/storyctl/"
echo "controller copied to $SC (story-ctl.py and the storyctl package)"
command -v python3 >/dev/null || echo "WARNING: python3 not found; the story agent needs Python 3"
command -v copilot >/dev/null || echo "WARNING: the Copilot CLI (copilot) is not on PATH; the story agent runs the agents through it"
command -v code >/dev/null || echo "NOTE: the 'code' command is not on PATH, so stops will not open in an editor tab by themselves (VS Code: Shell Command: Install 'code' command in PATH); use the link in the chat instead"
python3 "$SC/story-ctl.py" --help >/dev/null && echo "OK. In VS Code, open a new chat, choose the story agent and paste a story."
