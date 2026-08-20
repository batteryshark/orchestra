# Interruption doctrine

A question the system can answer is never asked.

## Three lanes

| Lane | When | What happens |
|---|---|---|
| **act** | The system has a move. | Do it. Do not display a choice. Do not ring. |
| **surface** | No judgment is needed now. | Show it on the board. Do not ring. |
| **interrupt** | A human must choose, and every option can resolve it. | File one card. Ring. |

There is no fourth lane. A dead end is silence, not a card.

## Decision procedure

A surface author runs these steps, in order, and stops at the first hit:

1. Can the system do the next move? → **act**.
2. Does anyone need to choose now? If no → **surface**.
3. Does every offered option dismiss or no-op? → stay silent.
4. Otherwise → **interrupt**. Scope the card to the project it is about.

`interrupt.decide` is this procedure. Call it before `nod.file_escalation`.

## Ledger

Each offense dies by a structural rule, not a policy note.

| Offense | Rule that kills the class | Enforced by |
|---|---|---|
| Dead-end card (Retry no-op + Leave pile-up) | No card without a resolving option. Dirty is not a stage: the merge lands. | `interrupt.has_way_out` + `nod._assert_actionable`; default `require_clean = false` |
| Obvious-answer card (resolver in 13s) | A known act is never an interrupt. First rebase/merge conflict is **act**. | `merge.at_completion` calls `interrupt.decide_merge`; test_merge_landing.py::test_a_rebase_conflict_dispatches_the_resolver_not_the_phone |
| Stale alarm as current state | Current fields never carry history. `outcome`/`error` are now; `last_error` is then. | `http.record_health`: `outcome=ok` ⇒ `error is None` |
| Noise drowning signal | Signal has a named layer. Filter in SQL, not on the client. | `GET /api/turns?layer=` |
| Notification scoped wrong | Pin or notify only the project named on the event. No project → pin nowhere. | `http._pinned_turns` requires `project_id` |
| "New build — restart" pill | The class has no host. `/api/service/update` does not exist. | absent route |

Phone rule: ring only on **interrupt**. Alerts that report (dismiss-only) stay on the alerts channel and do not use the decisions channel. Run start and run finish never buzz.
