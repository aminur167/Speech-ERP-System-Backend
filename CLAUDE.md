# Speech Therapy Lab — Backend

Django/DRF backend for a multi-branch speech therapy clinic ERP. The frontend (Next.js) lives at `E:\Speech ERP System` and is functionally complete but currently runs on mock data.

## Before writing any code

1. **Read `docs/00-OVERVIEW.md`** — stack decisions, branch-isolation rules, money-handling rules, offline/idempotency strategy, and the cross-cutting engineering checklist. It applies to every module.
2. **Read the specific module doc** for whatever you're building (`docs/01` … `docs/10`).
3. **Check `docs/ROADMAP.md`** for build order and the list of open questions that need the client's answer rather than a guess.

## How to treat the frontend

**The frontend was built as a client demo, not as a specification.** It shows what the product should feel like, and its `src/lib/api/*.ts` mock modules are a useful starting point for the API contract — but it is **not** authoritative on business correctness, security, or completeness. Expect gaps, missing fields, and places where the mock does something a real system must not.

- **Use it for:** what data each screen needs, response shapes, naming, and UX flow.
- **Do NOT treat it as:** proof that a rule is correct, or a reason to reproduce an unsafe pattern server-side.

**The backend is the system of record and must be designed properly on its own terms.** Where the mock is wrong, incomplete, or insecure, build it correctly and record the resulting frontend change in that module's doc — the frontend can be updated afterwards. Do not compromise backend design to avoid touching the frontend.

If something the UI needs simply isn't in the mock, that's an expected gap, not a blocker — design it correctly and note it.

## Non-negotiables

- **Branch isolation** — a Manager can only ever see/touch their own branch's data; Admin sees everything. Derived server-side from `request.user.branch`, never from a client-supplied param. Applies to detail endpoints, not just lists.
- **Money correctness** — `DecimalField` never float; `transaction.atomic()` around multi-step money operations; `select_for_update()` against race conditions; idempotency keys on payment writes.
- **Tests per module, not deferred** — a module isn't done until its own tests pass, covering happy path, edge cases, failures, branch isolation, and role permissions. Each module doc lists its expected cases; treat that list as a gate.
- **Built to last 10 years** — aggregate in the database, index deliberately, benchmark against realistically-sized data rather than demo rows. No shortcuts that trade correctness or scalability for delivery speed; if one is ever proposed, say so explicitly rather than letting it slide.
