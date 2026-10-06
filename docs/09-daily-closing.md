# 09 — Daily Closing (removed)

**Removed on 2026-10-01 at the client's request** — the clinic does not
reconcile the cash drawer against the system at the end of each day, so the
feature was taken out of both codebases: the `apps.dailyclosing` Django app,
its `/api/daily-closing/` endpoints, the Manager and Admin screens, the
dashboard card, the Reports mismatch section, the Summary tab, and the
Daily Ledger's closing column.

What changed as a consequence:

- **Voiding.** A Manager may still void only the same day's payments; the
  extra "not after the day's closing was submitted" cutoff went with the
  closing itself. Admin may void any day, as before. See `04`.
- **Today's Collection** on the dashboards comes from the transactions
  summary (`todayCollected`), counted the same way as Monthly Revenue.

**The data was kept.** Removing a Django app does not drop its tables, and no
migration was written to drop them, so every closing ever submitted — and
its amendments — is still in the database:

- `dailyclosing_dailyclosing`
- `dailyclosing_dailyclosingamendment`

They are no longer read or written by anything. If the clinic later decides
the history is not needed, dropping them is a deliberate, one-time database
operation (back up first); if the feature is ever wanted back, the code is
in git history before this commit and the tables are ready for it.
