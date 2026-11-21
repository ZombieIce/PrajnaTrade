# Worktrees

Create working trees **inside** the repository so a sandboxed session can write
to them:

```bash
git worktree add .worktrees/<name> -b <name> <base-branch>
```

Using `.worktrees/<name>` (instead of a sibling directory or `/tmp`) keeps the
checkout under the session workspace, where sandboxed writes are allowed.

- One branch per worktree. Do not check the same branch out in two worktrees;
  Git refuses it, and parallel edits to one branch defeat the isolation.
- Never check the integration branch (`zombieice-issue-17-implementation` /
  `main`) out twice. Create a new branch for the task and merge into the
  integration branch when done.
- Remove the worktree when finished: `git worktree remove .worktrees/<name>`.
  Use `git worktree prune` to clean up stale records after manual deletion.
- `.worktrees/` is gitignored. Worktree contents must never be committed; only
  the tracked files inside each worktree are part of its branch.
