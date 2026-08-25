# Git and collaboration rules

- Use `company/main` as the default company integration baseline unless the user selects another branch.
- Prefer a dedicated `.worktrees/<task-name>/` worktree for a new multi-file task. If the user has already placed active changes in a checkout, do not relocate or reset them mid-task without authorization.
- Before editing, inspect `git status`. Existing modified and untracked paths belong to the user unless clearly created for the current task.
- Each commit should represent one coherent concern. Stage explicit paths; never sweep unrelated files into a commit.
- Do not reformat unrelated code, rewrite history, delete branches/worktrees, or stop shared Docker stacks as cleanup.
- Commit only when requested or when the user has explicitly authorized the implementation workflow. Push, force-push, PR creation, tags, and deployment each require their own clear authorization.
- For status/update decisions, inspect the requested branch and remotes only; do not expand scope to unrelated branches.
