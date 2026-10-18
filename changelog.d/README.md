# Changelog fragments

Each PR with a user- or operator-visible change adds a fragment here instead of editing
`CHANGELOG.md`, so parallel PRs never conflict on it ([#139](https://github.com/ryansull/syncshelf/issues/139)).
`## [Unreleased]` in `CHANGELOG.md` stays empty on `main`.

## Writing one

- **Name:** `<issue-or-pr>.<type>.md`, for example `77.added.md`. Use the issue the PR closes,
  or the PR number when there's no issue.
- **Type:** the *Keep a Changelog* section it belongs in: `added`, `changed`, `removed` or
  `fixed`. A PR with changes of two types adds two fragments.
- **Content:** the changelog bullet(s) exactly as they should read: a `- ` bullet, continued
  on lines indented by two spaces, wrapped at 95 columns where the text allows. No blank lines
  and no trailing whitespace. Say what changed for the person running SyncShelf, and list
  every new config key and migration.

```markdown
- Expired OAuth rows are purged at every start and then every 24 hours (#77): authorization
  codes a day after they expire, access tokens once they expire, ...
```

A PR with no user- or operator-visible change (a refactor, internal tests, a docstring-only
fix) adds no fragment and is labelled `no-changelog`. CI fails a PR that changes `src/`
without either.

## Checking and releasing

- `uv run python scripts/changelog.py check` validates every fragment and that
  `## [Unreleased]` is empty. CI runs it.
- The release-prep PR runs
  `uv run python scripts/changelog.py build --version 0.3.0 --date 2026-11-01 --title "Phase 3: Hardcover."`,
  which writes the fragments under the new version heading in section order (Added, Changed,
  Removed, Fixed), ordered by number within a section, and deletes them.
