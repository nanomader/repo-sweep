#!/usr/bin/env bash
set -eu

usage() { echo "Usage: $0 <cc|codex> <repository-folder> <daily|weekly>" >&2; exit 1; }
[[ $# == 3 ]] || usage
agent=$1
repositories=$2
schedule=$3
[[ $schedule == daily || $schedule == weekly ]] || usage

prompts=$(cd "$(dirname "$0")/prompts" && pwd)
cd -- "$repositories"
task="Review the repositories in the current directory. Follow $prompts/$schedule.md and its linked instructions."

case "$agent" in
  codex) exec codex --yolo exec --skip-git-repo-check "$task" ;;
  cc)    exec claude --dangerously-skip-permissions -p "$task" ;;
  *)     usage ;;
esac
