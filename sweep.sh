#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ $# != 3 || ! $1 =~ ^(cc|codex)$ || ! -d $2 || ! $3 =~ ^(daily|weekly)$ ]]; then
  echo "Usage: $0 <cc|codex> <repository-folder> <daily|weekly>" >&2
  exit 1
fi
agent=$1
root=$(cd -- "$2" && pwd -P)
cadence=$3
project=$(cd -- "$(dirname -- "$0")" && pwd -P)
schedule="$project/prompts/$cadence.md"

if [[ $agent == cc ]]; then
  command=(claude -p --safe-mode --restricted --strict-mcp-config --no-session-persistence
           --tools Read,Grep,Glob --allowedTools Read,Grep,Glob --permission-mode dontAsk)
else
  command=(codex --no-daemon -a never exec --ignore-user-config --ignore-rules --ephemeral
           --skip-git-repo-check -c 'cli_auth_credentials_store="auto"' -c 'default_permissions="sweep"'
           -c 'permissions.sweep.filesystem={":minimal"="read",":workspace_roots"="read"}'
           -c 'permissions.sweep.network.enabled=false' -c 'shell_environment_policy.inherit="none"'
           -c 'project_doc_max_bytes=0' -c 'web_search="disabled"' --disable plugins --disable hooks
           --disable apps --disable multi_agent --enable skip_host_skill_discovery -)
fi
command -v "${command[0]}" >/dev/null || { echo "Install ${command[0]} first." >&2; exit 1; }

section() {
  awk -v title="$1" '/^## / { active = ($0 == "## " title); next } active && !/^Source: /' "$2"
}
checks=$(section Checks "$schedule" | sed -n 's/^- \[\([^]]*\)\](.*)$/\1/p')
[[ -n $checks ]] || { echo "No checks in $schedule" >&2; exit 1; }

state="${XDG_STATE_HOME:-$HOME/.local/state}/repo-sweep/shell"
mkdir -p "$state"
state=$(cd "$state" && pwd -P)
run=$(mktemp -d "$state/$cadence.XXXXXXXX")
work=$(mktemp -d)
work=$(cd "$work" && pwd -P)
trap 'rm -rf -- "$work"' EXIT
if [[ $cadence == daily ]]; then
  lock="$state/$(printf '%s' "$root" | git hash-object --stdin).lock"
  mkdir "$lock" 2>/dev/null || { echo "Daily run locked: $lock (remove if stale)" >&2; exit 1; }
  trap 'rmdir "$lock"; rm -rf -- "$work"' EXIT
fi
mkdir "$run/findings" "$work/repositories" "$work/heads"
cd "$work"

# Give both agents identical committed source, including line numbers, without running checkout hooks.
unset GIT_DIR GIT_COMMON_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES
export GIT_ALLOW_PROTOCOL= GIT_NO_LAZY_FETCH=1 GIT_TERMINAL_PROMPT=0
find "$root" \( -name node_modules -o -name .venv \) -prune -o -name .git -print0 -prune > checkouts
[[ -s checkouts ]] || { echo "No Git checkouts under $root" >&2; exit 1; }
while IFS= read -r -d '' marker; do
  repo=${marker%/.git}
  git_repo=(git --no-replace-objects -C "$repo")
  head=$("${git_repo[@]}" rev-parse --verify HEAD 2>/dev/null) || {
    echo "No readable HEAD in $repo" >> unavailable.txt; continue;
  }
  name=${repo#"$root"/}
  [[ $repo != "$root" ]] || name=${root##*/}
  branch=$("${git_repo[@]}" rev-parse --abbrev-ref HEAD)
  key=$(printf '%s\0' "$root" "$repo" "$branch" | git hash-object --stdin)
  files="repositories/$key"
  mkdir "$files"
  printf '%s\n%s\n' "$name" "$head" > "$files/revision"
  "${git_repo[@]}" grep -I -n --no-textconv -e '' "$head" -- > "$files/source.txt" || [[ $? == 1 ]]
  if [[ $cadence == daily ]]; then
    marker="$state/$key.base"
    empty=$("${git_repo[@]}" hash-object -t tree --stdin </dev/null)
    if [[ ! -f $marker ]]; then
      base=$("${git_repo[@]}" rev-list --first-parent -1 --before='24 hours ago' "$head")
      printf '%s\n' "${base:-$empty}" > "$run/$key.initial"
      ln "$run/$key.initial" "$marker" 2>/dev/null || [[ -f $marker ]]
      rm "$run/$key.initial"
    fi
    base=$(cat "$marker")
    "${git_repo[@]}" merge-base --is-ancestor "$base" "$head" 2>/dev/null || base=$empty
    "${git_repo[@]}" diff --no-ext-diff --no-textconv "$base" "$head" -- > "$files/changes.patch"
    printf '%s\n' "$head" > "heads/$key"
  fi
done < checkouts
rm checkouts
if [[ -d "$root/.repo-sweep-evidence" && ! -L "$root/.repo-sweep-evidence" ]]; then
  mkdir evidence
  for file in "$root"/.repo-sweep-evidence/*; do
    [[ -f $file && ! -L $file ]] || continue
    case "$file" in *.md|*.txt|*.log|*.json|*.sarif) cp "$file" evidence/ ;; esac
  done
fi

policy=$(section 'Policy — evidence and reporting' "$project/prompts.md")
scope=$(section Scope "$schedule")
final=$(section 'Consolidate — verify findings and write the report' "$project/prompts.md")
[[ -n $policy && -n $scope && -n $final ]] || { echo "Missing prompt or scope" >&2; exit 1; }
pids=()
while IFS= read -r title; do
  prompt=$(section "$title" "$project/prompts.md")
  [[ -n $prompt ]] || { echo "Unknown check: $title" >&2; exit 1; }
  id=$(printf '%s' "${title%% — *}" | tr '[:upper:] ' '[:lower:]-')
  note="$run/findings/$id.md"
  [[ $id =~ ^[a-z-]+$ && ! -e $note ]] || { echo "Invalid or repeated check: $title" >&2; exit 1; }
  touch "$note"
  echo "Reviewing $title" >&2
  (
    if ! printf '%s\n\n' "$policy" "$scope" "$title" "$prompt" | "${command[@]}" > "$note" 2>"$run/$id.log" ||
       ! grep -Eq '^Status: (COMPLETE|SKIPPED|PARTIAL)[[:space:]]*$' "$note"; then
      printf '\nStatus: PARTIAL\nCoverage: Agent failed or returned an invalid note.\n' >> "$note"
    fi
  ) &
  pids+=("$!")
  if [[ ${#pids[@]} == 3 ]]; then
    for pid in "${pids[@]}"; do wait "$pid"; done
    pids=()
  fi
done <<< "$checks"
wait

cp -R "$run/findings" .
printf '%s\n' "$final" | "${command[@]}" > "$run/report.tmp" 2>"$run/consolidate.log" || {
  echo "Report generation failed. See $run/consolidate.log" >&2; exit 2;
}
grep -iq '<html[ >]' "$run/report.tmp" && grep -iq '</html>' "$run/report.tmp" || {
  echo "Agent returned an incomplete HTML document. Notes: $run/findings" >&2; exit 2;
}
mv "$run/report.tmp" "$run/report.html"
printf '%s\n' "$run/report.html"
if [[ -f unavailable.txt ]] || grep -Eq '^Status: PARTIAL' "$run"/findings/*.md; then exit 2; fi
for head_file in heads/*; do
  [[ -f $head_file ]] || continue
  key=${head_file##*/}
  cp "$head_file" "$run/$key.next"
  mv "$run/$key.next" "$state/$key.base"
done
