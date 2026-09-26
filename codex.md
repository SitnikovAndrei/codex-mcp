
# Codex as executor

Claude Code is the brain (understands the task, plans, reviews); Codex is the executor via the `codex` MCP server (`mcp__codex__*`).

- `codex_task` — delegate implementation work: well-defined edits, refactors, writing tests, fixing failing checks. Write a self-contained prompt (goal, relevant files, constraints, how to verify). Always pass `cwd` = current project root.
- Continue the same Codex thread with `thread_id` from the report for fixes/follow-ups instead of re-explaining.
- Independent subtasks can be sent as parallel `codex_task` calls.
- After each task, review the actual diff (`git diff`) and verify the result yourself; send corrections back via `thread_id`.
- Long tasks: `codex_task(wait=false)`, then `codex_status(wait_s=...)`; adjust mid-run with `codex_steer`, stop with `codex_interrupt`.
- Status `waitingForAnswer` = Codex asked a question: answer it yourself with `codex_answer` if the task context allows; ask the user only when it's genuinely their decision.
- Pass screenshots/mockups via `codex_task(images=[...])`. Check `codex_limits` before large or parallel delegations.
- `codex_thread_manage`: `revert` drops turns from history only (files stay changed); `goal_set` makes Codex start working on its own — follow with `codex_status`.
- `codex_capabilities` / `codex_mcp_call` reach Codex's own MCP servers and plugins; prefer your own tools when they cover the need.
- Pick model/effort per task (`codex_models` lists them): cheaper model + low effort for mechanical work, stronger for hard work.
- `codex_review` gives a second-opinion review of uncommitted changes / a branch / a commit.
- Sessions: `codex_threads` (filter by `cwd`), `codex_thread_read`, `codex_fork` for alternative approaches, `codex_thread_manage` to rename/archive/compact.
- `codex_exec` — run shell commands in Codex's sandbox when the user asks to execute through Codex.
- Do it yourself (no delegation) for exploration, small one-line edits, questions, and planning.
