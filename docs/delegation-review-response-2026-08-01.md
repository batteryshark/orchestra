# Response to the delegation review

Thanks for the concrete field report. All five findings have now been addressed.

1. **Non-Git child spawning:** `orchestra spawn` now falls back to the lead's
   shared workdir with an explicit warning when an isolated Git worktree is not
   available. The delegation is no longer dropped.

2. **Dispatch dependencies:** `orchestra dispatch --after <run-id>` is durable,
   repeatable, visible as `pending-on-N`, and transactionally claimed so it
   fires once. Pending dispatches can be stopped with `orchestra cancel`; a
   failed, timed-out, or cancelled prerequisite declines the consumer without
   launching it. Worktree creation and the execution timeout now begin only
   when the dependent run actually fires.

3. **Blocking child consultation:** supervised workers, including spawned
   children, can use
   `orchestra consult "<question>" --wait <seconds> --fallback "<assumption>"`.
   The existing child-to-lead route is retained, ordinary consultation remains
   non-blocking, and the mandatory fallback is applied when the bounded wait
   expires.

4. **Tier-bounded spawning:** roster profiles may set an integer `tier`. When
   both profiles are tiered, a child cannot exceed its parent's tier; the error
   directs the worker to consult its requester. Omitting either tier preserves
   the previous unconstrained behavior.

5. **File-backed interrupts:** `orchestra interrupt <run> --file <path>` now
   mirrors `send --file`, preserving complete multiline UTF-8 corrections
   without shell quoting or expansion.

The implementation also recovers stale pre-launch dependency claims instead of
leaving them stuck in `spawning`. Documentation and generated worker/playbook
guidance were updated. Verification completed with **486 tests passing and 2
skipped**, plus Python compilation and `git diff --check`.
