# PR Test Receipt Templates

Always return the response in two distinct parts:

1. **Developer Preflight Summary (outside the code block)** — Short, actionable verdict for the author:
   - Verdict (`Ready for review`, `Changes needed`, or `Incomplete verification`) and one-sentence reason.
   - **Blockers** (omit section if none): `file:line`, concrete impact, smallest next step.
   - **Scope / Snapshot Warnings** (omit if clean & unambiguous): dirty-tree overlap, stale base, mid-run tree changes, or unverified remote CI gates.
2. **Paste-Ready PR Test Receipt (inside a single fenced ` ```markdown ` code block)** — Written cleanly for PR reviewers, ready to paste verbatim into GitHub/GitLab.

Keep the paste-ready receipt compact by default. Use `<details>` for exact commands and failure excerpts so large PRs stay scannable. If the repository has a `.github/pull_request_template.md` (or equivalent) with a Testing/Verification section, adapt the heading and checklist items to match it—only check boxes verified in this session.

---

## Template A: Clean / Standard Receipt (Default)

Use when the working tree is clean and verified checks cover the intended committed PR scope.

```markdown
### PR Test Receipt

**Verdict:** Ready for review · **Scope:** `<branch>` (`<head_short>`) vs `<base_ref>` (`<base_short>`, local snapshot) · **Working tree:** Clean

| Check | Snapshot | Result | Observed |
| --- | --- | --- | --- |
| `<short label>` | Committed `<head_short>` | PASS | `<counts / short result>` |
| `<CI-only gate>` | Remote CI | NOT RUN | `<reason, e.g. requires staging credentials>` |

<details>
<summary>Exact commands & verification notes</summary>

- `<exact command>` → exit `0` (`<short observed result>`, `<duration>`)
- Post-check verification: working tree remained clean at `<head_short>`.
- Remote CI status is not verified locally.

</details>
```

---

## Template B: Dirty-Tree Split Receipt (Working Tree vs. Committed HEAD)

Use when the working tree is dirty and you verified committed `HEAD` separately (e.g., via `scripts/verify_head_worktree.py`), OR when local checks ran only on the dirty checkout.

### B1. Side-by-side comparison (isolated committed `HEAD` also ran)

```markdown
### PR Test Receipt

**Verdict:** Changes needed · **Scope:** `<branch>` (`<head_short>`) vs `<base_ref>` (`<base_short>`, local snapshot) · **Working tree:** Dirty (`<N>` uncommitted file(s), overlaps PR diff)

| Check | Working tree (dirty) | Committed `<head_short>` | Gap / Impact |
| --- | --- | --- | --- |
| `<short label>` | PASS (`<counts>`) | FAIL (`<counts>`) | `<uncommitted file(s)>` holds the fix; committed `HEAD` still fails `<failing test>` |

**Next step before review:**
- Stage and commit `<path:line>`, then re-run `<command>` on the clean commit.

<details>
<summary>Exact commands & observed output</summary>

- Working tree (`<head_short>` + `<staged/unstaged summary>`): `<exact command>` → exit `0` (`<observed>`)
- Isolated committed `HEAD` (`<head_short>`): `<exact command>` → exit `1` (`<short redacted failure summary>`)

</details>
```

### B2. Dirty working tree only (isolated committed `HEAD` could not run)

```markdown
### PR Test Receipt

**Verdict:** Incomplete verification · **Scope:** `<branch>` (`<head_short>`) vs `<base_ref>` (`<base_short>`, local snapshot) · **Working tree:** Dirty (`<staged/unstaged/untracked summary>`)

| Check | Snapshot Tested | Result | Gap / Reason |
| --- | --- | --- | --- |
| `<short label>` | Working tree (dirty) | PASS (`<counts>`) | Does **not** verify committed `<head_short>` alone |
| `<short label>` | Committed `<head_short>` | NOT RUN | `<e.g. isolated worktree missing gitignored node_modules>` |

**Next step before review:**
- Commit or stash `<dirty files>`, then re-run `<exact command>` on the clean checkout.

<details>
<summary>Exact commands & verification notes</summary>

- Working tree (`<head_short>` + dirty changes): `<exact command>` → exit `0` (`<observed>`)
- Isolated committed `HEAD` (`<head_short>`): blocked — `<exact reason>`

</details>
```

---

## Template C: Incomplete Verification / Zero-Test Trap / Uncertain Base

Use when base resolution is uncertain, a command matched zero tests or skipped the relevant case, or a required local gate could not run.

```markdown
### PR Test Receipt

**Verdict:** Incomplete verification · **Scope:** `<branch or DETACHED>` (`<head_short>`) vs `<base_ref or unverified base>` · **Working tree:** `<Clean or Dirty>`

| Check | Snapshot | Result | Observed / Reason |
| --- | --- | --- | --- |
| `<short label>` | `<snapshot>` | INCOMPLETE | Command exited `0` but matched `0` tests (or skipped target case) |
| `<required gate>` | Local | NOT RUN | `<reason>` |

**Most useful next check:**
- `<exact single highest-value command or git check to close the gap>`

<details>
<summary>Exact commands & verification notes</summary>

- `<exact command>` → exit `<code>` (`<observed>`)
- Scope note: `<candidate/missing base ref or snapshot caveat>`

</details>
```

---

## Formatting Rules

- Keep table cells short and scannable; put full command strings and redacted error summaries inside `<details>`.
- Always include exact test counts when the runner outputs them (e.g., `12 passed, 0 failed, 1 skipped`)—never write vague claims like "all tests pass".
- Distinguish **PASS**, **FAIL**, **INCOMPLETE** (e.g., 0 tests ran / target test skipped / tree mutated mid-run), and **NOT RUN**.
- Never include credentials, tokens, private URLs, or `.env` values.
- Match the user's language for headings and prose while preserving exact branch names, SHAs, file paths, and commands.
