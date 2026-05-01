---
name: fix-review
description: Run the Codex-review fix loop. For each finding: understand root cause, implement a surgical fix, run the full pytest suite, update CLAUDE.md §N if architectural, update README if user-visible, commit per logical chunk, push, and ask before deploy. The README sync step is mandatory and must not be skipped.
---

# fix-review

End-to-end playbook for addressing a round of review feedback (Codex, human PR reviewer, or self-review) without dropping the docs step. This is the exact loop this project has used successfully; codifying it keeps each iteration consistent.

## When to invoke

The user pastes one or more review findings (P1/P2/P3 labels, code-comment blocks, bullet lists). They expect you to work through every finding, ship fixes, and return with a clean delta.

If the review is trivial (≤1 finding, single-line fix), skip the skill's overhead and just fix it directly.

## Phase 1 — Understand each finding

1. **Parse the findings.** For each one: extract the priority (P1/P2/P3), file/line reference, the claim, and the suggested fix if any.
2. **Verify before fixing.** Do NOT trust the finding's code references blindly. Read the cited file + lines with the Read tool. Confirm the bug exists and understand the current behavior. A "finding" that's already been fixed (or was never a bug) still needs explicit acknowledgment, not a blind edit.
3. **Group findings.** If two findings touch the same file/function, fix them in one commit. If they're in disjoint areas, plan separate commits.
4. **Explicitly skip cosmetics.** If a finding is stylistic (comment wording, variable rename with no behavior change) AND the user hasn't flagged it as important, note it but don't fix — return the time to real issues.

## Phase 2 — Fix each finding

For each grouped change:

1. Write the fix (Edit tool). Keep edits surgical — don't refactor adjacent code unless the fix requires it.
2. Add a test that reproduces the bug pre-fix, if possible. The project's pattern is to write tests that are named after the exact failure mode (e.g. `test_lifecycle_low_does_not_silence_mother_returned`).
3. Run the test suite incrementally for the affected module first (`pytest tests/test_events.py`) before the full run.

## Phase 3 — Run the full test suite

Always, before committing any fix:

```bash
source venv/bin/activate && TEST_MODE=true python -m pytest tests/ --tb=short
```

Acceptance criterion: **same functional pass set as before, or larger**. If tests drop, stop — either the fix is wrong or an existing test is revealing a real conflict. Do NOT commit with a reduced pass count.

Known exception: in sandboxed environments without network access, 2 Discord-integration tests may fail (`tests/integration/test_analytics_and_feed.py`). Treat that as "same as before" if the count matches.

## Phase 4 — Update documentation

This is the step that most commonly gets dropped. Do it before committing.

1. **CLAUDE.md** — if the fix introduces a new rule, invariant, config flag, data-model field, or architectural constraint that a future contributor needs to respect, add a new numbered section (e.g. §30) under "Hard-won knowledge". Follow the existing §23 / §29 format: incident → root cause → fix → "Don't regress this" rules → test coverage. Keep it technical and terse.

2. **README.md** — if the fix is user-visible (new config flag, changed default, new alert category, test count changed, channels added/removed), update:
   - The "Config that matters" table if a flag was added
   - The "By the numbers" table if test count changed (currently 194)
   - The "Tech stack" and "Run the tests" snippets if they reference test counts
   - Any narrative section (voice: NYT-feature, first person, no em-dashes, no new emojis) only if the user-visible behavior fundamentally changed. Don't add war-story sections for internal refactors — those belong in CLAUDE.md, not the README.

3. **Skip docs only if** the fix is a pure internal refactor with no new rules, no config changes, no user-visible behavior change, and the test count didn't move.

## Phase 5 — Commit

One commit per logical chunk (not one per finding if two findings share a fix). Use a descriptive title ≤70 chars, followed by a body that:
- Names each finding addressed (with P1/P2/P3 label)
- Explains the root cause briefly
- Notes the test count (pre + post)
- Ends with: `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`

Use HEREDOC to preserve formatting:

```bash
git commit -m "$(cat <<'EOF'
<title>

<body>

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

## Phase 6 — Push and verify

1. `git push origin main`
2. `git log origin/main..HEAD --oneline` — confirm nothing left local.
3. `git status --short` — confirm clean working tree (untracked `AGENTS.md` is expected and fine).

## Phase 7 — Deploy decision

**Do NOT deploy without explicit user confirmation.** This project has a deployed launchd service and the user decides deploy timing.

When asking, give the shape of the change so they can decide:
- "Analyzer-only restart needed (rules/prompt change)" → `launchctl kickstart -k gui/$(id -u)/com.birdnest.analyzer`
- "Both services needed (shared code in state.py or config.py)" → both kickstarts
- "No restart needed (log-only / test-only change)" — note this explicitly

If they approve, deploy + post-deploy verify:
1. `launchctl list | grep birdnest` → confirm new PIDs, exit code 0
2. Tail logs for ~60s or check for post-deploy errors via timestamp filtering
3. Report clean boot + first-snap success

## Phase 8 — Close the loop

Report to the user:
- Number of findings addressed (and any explicitly skipped + why)
- Commits landed (short hashes)
- Test count pre + post
- Whether docs were updated (CLAUDE.md §N, README sections)
- Whether deploy happened + post-deploy status
- Any open follow-ups (new tasks, known limitations)

## Anti-patterns to avoid

- **Skipping the README.** If the review changed a config flag or added a new config row, the "Config that matters" table needs to show it. This step has been dropped repeatedly in past sessions.
- **Batch-committing unrelated findings.** Makes `git log` useless for bisection.
- **Deploying without asking.** The user owns deploy timing.
- **Fixing cosmetics the reviewer didn't flag.** Scope creep on a review pass.
- **Updating `.expected.json` files in regression suites** to make a failing test pass. The test fails because behavior changed; either the change is wrong or the expected needs user approval to update.
- **Assuming review findings are correct.** Read the cited code before writing any fix.
