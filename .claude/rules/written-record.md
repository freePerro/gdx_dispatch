---
paths:
  - "**/*.md"
---

# The written record

Moved here from `CLAUDE.md` on 2026-10-11 so it loads when a matching file is read or written with the Read or Write tool, or read with a plain `cat`/`head` of the file — not on pipes, `wc`, Bash edits (`sed -i`, heredocs, scripts), the Grep tool or a directory-wide search, and not in every session's preamble.

Every doc is either **about the past** or **about the present**, and the two
have opposite maintenance rules. A design doc, an ADR, an audit records what was
decided and why; it stays true forever, because the past does not change. A
guide, a runbook, an invariant registry, a "what's left" tracker describes the
system as it is right now, and starts rotting the day it is written.

**Date-stamp the past. Fix or retire the present.** This is not a preference —
it predicts where the defects are. The 2026-09-01 doc audit found ten live
defects and **all ten came from present-tense docs**: guides, runbooks, an ADR
whose status was left behind by its own build commit, and two root trackers.
**Zero came from a completed design doc.** No doc in this repo has ever
overclaimed; the record only ever undersells what exists.

- **Every doc carries a status line in its header block — plans, guides,
  runbooks and ADRs alike.** Line 3, or just below it when a `**Date:**` block
  comes first. Vocabulary: `PLAN` · `PARTIALLY BUILT` · `MERGED #N` ·
  `RELEASED vX.Y.Z` · `HISTORICAL`. It names what is *not* built when the
  answer is "some of it". A doc with no status line is incomplete. Measured
  2026-09-12 over tracked files: `docs/design/` is 70 of 70 and
  `gdx_dispatch/docs/` is 41 of 41. Every doc complies today; the rule is what
  keeps it that way.
- **The status line ships with the code.** A PR that implements part of a plan
  updates that plan's status in the same PR. ADR-016 was edited *inside its own
  build commit* and still read "nothing built yet" while the feature sat in the
  sidebar — that is the failure this rule exists to stop.
- **Docs state; code proves.** Never cite a plan as evidence of current state —
  not its status line, and especially not its "what already exists" table. Two
  such tables in this repo were wrong, one of them on the day it was written.
  Re-verify against code, a PR number, or a release tag before acting.
- **Half-shipped is not shipped.** When a multi-part fix lands partially, the
  record names which parts. "M4 fixed" for a fix that landed in one of two
  files is worse than no note at all.
- **Check for a rival plan first.** Before writing a plan, grep `docs/design/`
  recursively (finished records live in `docs/design/archive/` since
  2026-09-06) for others naming the same files. If one exists, cite it or mark it
  superseded — in both docs. Two plans in this repo reached opposite decisions
  about the same money path without ever referencing each other.
- **Keep the past; retire the present.** A shipped plan stays — its rejected
  alternatives and audit findings are the part code cannot recover, and
  deleting one manufactures the dead references this repo audits for. A
  present-tense doc whose subject no longer exists is the opposite case: it
  carries no reasoning, only instructions for a system that isn't there. Give
  it a `HISTORICAL` status line saying what it described and that the thing was
  never built. Deletion is available for that class and that class only, and
  only when the doc holds no decision anyone could still need.
- **Open a plan with "what already exists (do not rebuild)."** The best doc in
  the corpus established that half its ask needed no code at all.
