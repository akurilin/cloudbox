# Agent instructions

- When implementing changes to the codebase, work using a supervisor/verifier + worker sub-agents pattern where the work split makes sense.
- Work directly in main unless asked otherwise

## Session start

- Code and tests define behavior; `README.md` covers usage and future work.
- Inspect current files and preserve unrelated changes.
- Distinguish planning, local implementation, and AWS deployment. AWS login is
  not deployment approval.

## Test environment

Keep the test environment deployed during implementation. Update its resources
and worker image as needed. Do not reset or tear it down after each change.
Stop completed job VMs; remove the environment when requested or when testing
the full setup/teardown lifecycle.

## Before a commit

Run `uv run pre-commit run --all-files` only immediately before a commit.
Do not run the suite, linters, or formatters during routine edits or reviews.
If a hook changes files, review and stage the changes, then rerun before committing.
Use focused tests during implementation when behavior changes.

## Full cloud infra + basic task test

Run `scripts/e2e_cloud.py` (infrastructure and math) manually, only on user request.
It is slow. Do not run or require it for commits, pushes, hooks, or routine CI.
