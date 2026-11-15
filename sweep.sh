#!/usr/bin/env bash
set -e

if [[ $# -ne 3 ]]; then
  echo "Usage: $0 <cc|codex> <repository-folder> <daily|weekly>" >&2
  exit 1
fi

agent=$1
repository_folder=$2
schedule=$3

if [[ $schedule != daily && $schedule != weekly ]]; then
  echo "Choose daily or weekly." >&2
  exit 1
fi

# Find the schedule before switching to the folder being reviewed.
prompt_folder=$(cd -- "$(dirname -- "$0")/prompts" && pwd)
schedule_file="$prompt_folder/$schedule.md"
cd -- "$repository_folder"

case "$agent" in
  cc)
    exec claude --dangerously-skip-permissions -p "Follow '$schedule_file'."
    ;;
  codex)
    exec codex --yolo exec --skip-git-repo-check "Follow '$schedule_file'."
    ;;
  *)
    echo "Choose cc or codex." >&2
    exit 1
    ;;
esac
