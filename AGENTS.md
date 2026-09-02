# Agent instructions

- When implementing changes to the codebase, work using a supervisor/verifier + worker sub-agents pattern where the work split makes sense.
- Work directly in main unless asked otherwise

## Session start

- Code and tests define behavior; `README.md` covers usage and future work.
- Inspect current files and preserve unrelated changes.
- Distinguish planning, local implementation, and AWS deployment. AWS login is
  not deployment approval.

## Full cloud infra + basic task test

Run `scripts/e2e_cloud.py` (infrastructure and math) manually, only on user request.
It is slow. Do not run or require it for commits, pushes, hooks, or routine CI.
