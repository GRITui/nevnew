#!/usr/bin/env bash
#
# DEPRECATED 2026-09-06 — minimax/minimax-m3:free is no longer the offload
# worker. Kept as a thin compatibility wrapper so existing references
# (BACKLOG.md, notes, muscle memory) keep working. All tasks now dispatch
# through scripts/offload.sh — the tiered dispatcher (small: qwen3.7-plus,
# code: deepseek-v4-pro, complex: mimo-v2.5-pro). Default class: code.
#
# Usage (unchanged):  ./scripts/offload_to_minimax.sh [-f task_file] "task"
#                     echo "task" | ./scripts/offload_to_minimax.sh

exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/offload.sh" -c code "$@"
