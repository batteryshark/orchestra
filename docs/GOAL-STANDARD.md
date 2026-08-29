<!-- Vendored copy of the craft-goal skill's STANDARD.md, taken 2026-08-25. The refine lane's brief points every
refinement run at THIS file (orchestra/refine.py), so the standard ships with
Orchestra instead of depending on a skill checkout. Re-copy when the source
changes; never edit this copy. -->

# The Goal Standard

What a goal or epic must contain before an executor — human or agent — can
run with it and not guess. `craft-goal` builds to this standard, `grade-goal`
scores against it, `split-goal` refuses input that fails it. The standard is
tool-neutral: it describes a markdown document. Mapping onto a tracker
is a thin layer at the end.

## Vocabulary

A **goal** states one bounded outcome. An **epic** is a goal big enough to
need child items. Same shape, same sections — the only difference is that an
epic ends with children.

## The document shape

A crafted goal is one markdown document with these sections, in this order:

```
## Description         situation and root cause; ends with Out of scope
## Goal                one sentence: outcome + "so that"
## Requirements        - [ ] properties the solution must have
## Acceptance Criteria - [ ] observable tests, each with a verification method
## Plan                fat-marker sketch; epics: the slice sketch
## Notes               Appetite, decisions, Q:/A: open questions
```

## The four properties

A goal is bounded when it carries all four: **purpose, boundary, budget,
proof**. Each lives in a fixed place.

### Purpose — why, so ties break your way

- `## Goal`: one sentence. The outcome plus a "so that" clause naming who it
  serves. Outcome means a changed state of the world, not an activity — if
  the sentence is a verb phrase like "migrate X", ask what is true afterwards.
- `## Description`: the situation and root cause, in the author's own words.
  Preserve the original phrasing that started the item; clarify around it,
  never replace it.

### Boundary — the fence against helpful drift

- **Out of scope**, at the end of `## Description`: 2–3 explicit exclusions,
  the adjacent things a helpful executor would otherwise wander into. This
  list does more work than the in-scope list. Zero exclusions means the
  boundary was never drawn.
- `## Plan`: the fat-marker sketch — main flows, files or modules expected to
  change, the existing pattern to follow ("do it like the X handler"). Point
  at ground truth; never paste code or schemas that will drift stale.
- Decisions already made (library, naming, approach) and decisions expressly
  left to the executor ("either is fine, pick one and note it") go in
  `## Notes`.
- Dependencies — what must exist first, and territory shared with in-flight
  work — get named in `## Notes`.

### Budget — the stopping rule

- One `Appetite:` line in `## Notes`: what the work is worth in time ("two
  evenings", "a week"), plus what gets cut first when it runs tight. An
  executor with no appetite gold-plates forever.
- Known-hard sub-problems (rabbit holes) are patched in `## Plan` before the
  goal ships: a declared simplification, a cut, or a spike-first tripwire
  ("if X takes more than Y, stop and report"). "We'll figure out X" never
  ships.

### Proof — done is a fact, not a feeling

- `## Requirements`: properties the solution must have. Rationale may ride
  inline.
- `## Acceptance Criteria`: externally observable end-to-end tests, each with
  its verification method — a command to run, a behavior to observe ("with
  the laptop off, a capture typed on the phone lands in the home inbox").
  A criterion nobody can mechanically check is a criterion the executor will
  claim is met.

**Fewer, sharper.** Every checkbox is a promise someone must later tick or
decline with a reason. At most 6 requirements and 5 acceptance criteria —
one condition each, short declarative sentence, no trailing period.

## Failure modes

Name these when grading; each detect line is mechanical.

| Failure | Detect |
|---|---|
| Mirage outcome | No sentence says what is true afterwards |
| Frankenstein goal | "and" joins two outcomes with different success tests |
| Missing out-of-scope | Zero explicit exclusions anywhere |
| Unfalsifiable criteria | A criterion no command or observation settles |
| Solutioneering | Method prescribed, "why" absent — no trade-off is decidable |
| No appetite | Nothing says when to stop polishing |
| Rabbit hole unpatched | "We'll figure out X" left in scope |
| Iceberg | Enumerating touched states/cases keeps growing the list |
| Grab-bag epic | Children share a theme but no demoable whole |
| Stale-context brief | Pasted code or schema instead of a pointer to it |

## The ready test

A goal is bounded when every question below is answered in the document or
explicitly left to the executor:

1. Outcome — what is true afterwards, and how observed
2. Why — what breaks or is lost if skipped
3. Out of scope — what a helpful executor must not touch
4. Appetite — worth how much, cut what first
5. Constraints and decided decisions — what is already fixed
6. Dependencies — what must exist first, who shares the territory
7. Rabbit holes — the hard 10%, patched
8. Proof — every criterion checkable, method named

Open questions live in `## Notes` as `Q:` lines, resolved by editing the line
to `A:`; an unanswered `Q:` fails the test. An empty section beats a
fabricated one.

## Epic extras

An epic additionally sketches its slices in `## Plan`: 3–7 candidate
children, each a vertical slice — demoable end-to-end on its own, never a
layer ("the DB part"). Children carry a one-line pointer to the parent's
purpose so the executor can break ties without asking. Slices are candidates;
execution may reshape them.

## Tracker mapping

When the item lives in a tracker, the document above maps 1:1 onto a
tracker task — the sections keep their names and order; the tracker adds `Progress Log`
and `Completion Summary`, which this standard leaves alone.

- A goal is a top-level task (`parentId: null`); an epic's children are tasks
  with `parentId` set to it. One level only.
- The checkbox economics are enforced: the tracker refuses `review` and `blocked`
  while any checkbox is neither ticked nor declined-with-a-reason.
- A goal enters execution only when a human ticks `delegated` and moves it
  `backlog → ready`. No skill does either.
