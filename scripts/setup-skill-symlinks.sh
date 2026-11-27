#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source_dir="$HOME/SyncThing/repo-specific-skills/personal/mypersonalfinance"
destinations=(
    "$repo_root/.pi/skills"
    "$repo_root/.claude/skills"
    "$repo_root/.agents/skills"
)

if [[ ! -d "$source_dir" ]]; then
    printf 'Source directory not found: %s\n' "$source_dir" >&2
    exit 1
fi

status=0
for destination in "${destinations[@]}"; do
    mkdir -p -- "$(dirname -- "$destination")"
    if [[ -L "$destination" && "$(readlink "$destination")" == "$source_dir" ]]; then
        continue
    fi
    if [[ -e "$destination" || -L "$destination" ]]; then
        printf 'Refusing to replace %s\n' "$destination" >&2
        status=1
    elif ! ln -s -n -- "$source_dir" "$destination"; then
        status=1
    fi
done
exit "$status"
