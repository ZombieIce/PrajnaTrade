# Issue tracker: GitHub

GitHub Issues are the source of truth for specs and tickets in this repository. Use the `gh` CLI for issue operations.

## Conventions

- Create an issue with `gh issue create --title "..." --body "..."`.
- Read an issue with `gh issue view <number> --json number,title,body,labels,comments`.
- List issues with `gh issue list` and the appropriate `--state` and `--label` filters.
- Apply or remove labels with `gh issue edit <number> --add-label "..."` or `--remove-label "..."`.
- Use native issue dependency relationships when available; otherwise state blockers in the issue body.

## Pull requests as a triage surface

**PRs as a request surface: no.**

When an engineering skill says to publish to the issue tracker, create a GitHub issue. Infer the repository from `git remote -v`.
