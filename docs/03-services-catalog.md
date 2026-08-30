# 03 — Services Catalog

Depends on `01-auth-and-branches.md`.

Read `docs/00-OVERVIEW.md` first for the cross-cutting rules.

Frontend source: `src/lib/api/services.ts`, `src/lib/api/serviceEnrollment.ts`, `src/components/services/ServiceCard.tsx`, `ServiceForm.tsx`, `ServiceCatalogView.tsx`.

---

## Model: Service

**Services are NOT branch-scoped.** This is the one major exception to the branch-isolation rule — the catalog is shared organization-wide, so every branch offers the same packages at the same prices. Managers see the catalog read-only; only Admin can create/edit/delete. (Contrast with Materials in `06`, which *are* per-branch because physical stock doesn't move between locations.)

| Field | Type | Notes |
|---|---|---|
| `name` | CharField | e.g. "Monthly 1:1 Individual Plan" |
| `code` | CharField, unique | e.g. `PKG-M1-01` |
| `category` | choices: `daily` \| `monthly` \| `installment` \| `online` | Drives which enrollment wizard uses it |
| `fee` | **DecimalField** | Current price — money rule applies |
| `is_online` | bool | |
| `description` | TextField, optional | |
| `original_fee` | DecimalField, optional | Pre-discount price; UI strikes it through when > `fee` |
| `duration_label` | CharField, optional | Free text: "1 Month (auto-renew)", "60 mins / visit" |
| `sessions_label` | CharField, optional | Free text: "12 Sessions" |
| `expiry_label` | CharField, optional | Free text: "3 months from purchase" |
| `is_active` | bool, default `True` | 🆕 Retired from sale but still billing existing patients — see below |
| `is_deleted` | bool | Soft delete — see below |

The three `*_label` fields are deliberately **free text, not structured data**. They're descriptive copy shown on the package card, not values the system computes with. Don't over-model them into durations/intervals — nothing in the app parses them.

### ✅ CONFIRMED: `registration_fee` is dropped entirely

**Registering a patient is free.** Patients pay only for the services they actually use, at that service's price. There is no signup or membership charge anywhere in the system.

So: **do not build `registration_fee`.** No model field, no serializer field, no card tile, no receipt line.

The frontend mock currently shows a `registrationFee` on package cards (৳1,000 on "Monthly 1:1 Individual Plan", "Free" on the others) but never charges it. A money field that's displayed but never collected is worse than no field at all — staff eventually assume it was charged when it wasn't, and reconciliation quietly drifts.

**Frontend follow-up:** remove `registrationFee` from the `Service` type, `ServiceForm`, the `ServiceCard` info grid, and the seeded packages. Not done yet.

If the clinic ever wants a one-time signup charge later, add it deliberately as a real charge with its own receipt line — not as decoration.

### ✅ CONFIRMED: deletion is blocked while the package is in use; deactivation is the alternative

Two separate concepts, and the distinction matters:

| Action | What it means | When allowed |
|---|---|---|
| **Deactivate** (`is_active = False`) | Package is retired from sale. Existing enrollments keep billing normally; nobody new can enroll. | Always |
| **Delete** (`is_deleted = True`, soft) | Package was a mistake and should disappear from the catalog entirely. | **Only when no active enrollment or plan references it** |

**Delete is refused while in use.** If any non-terminated `MonthlyEnrollment` or `InstallmentPlan` points at the service, return a 400 whose message states **how many** patients are affected and points to deactivation — e.g.:

> *"5 patients are currently enrolled in this package. Deactivate it instead to stop new enrollments while existing patients keep their plans."*

The count is the important part: "cannot delete" alone leaves the Admin stuck with no idea why or what to do next. Include an `activeEnrollmentCount` in the error payload so the UI can offer a "Deactivate instead" button directly.

**Add `is_active` to the model** (separate from `is_deleted`):
- `is_active = False` → hidden from the enrollment wizards' service pickers, so no new enrollments; **still visible in the Admin catalog** marked "Inactive", so it can be reactivated.
- Existing enrollments on an inactive service are unaffected — bills keep generating, payments keep working. Deactivating must never break someone's ongoing plan.
- `is_deleted = True` → gone from every list, but FK references still resolve so past receipts and history stay intact (never hard-delete).

**Frontend follow-up:** an Active/Inactive toggle on the package card, "Inactive" badge in the Admin catalog, inactive packages filtered out of the four enrollment wizards, and a delete-blocked dialog offering deactivation. Not built yet.

---

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/services/` | `?category=` optional. Both roles (Managers need it in enrollment wizards) |
| POST | `/services/` | **Admin only** |
| GET | `/services/{id}/` | |
| PUT | `/services/{id}/` | **Admin only** |
| DELETE | `/services/{id}/` | **Admin only**, soft delete — refused while enrollments are active |
| POST | `/services/{id}/deactivate/` | **Admin only** — retire from sale |
| POST | `/services/{id}/activate/` | **Admin only** — put back on sale |
| GET | `/services/enrollment-counts/` | `{ [serviceId]: count }` |

`GET /services/?category=` is called by the enrollment wizards and must return **only active** services. The Admin catalog view needs inactive ones too — support `?includeInactive=true` (Admin only) rather than two different endpoints.

**Role split is real, not cosmetic:** `ServiceCatalogView` is rendered for both roles with a `canManage` flag — Admin gets Edit/Delete buttons, Manager doesn't. Enforce server-side; don't rely on the hidden buttons.

`enrollment-counts` returns active enrollment + active plan counts per service, powering the "N enrolled" line on package cards. Compute with a bulk aggregate (`values().annotate(Count(...))`), not a per-service loop. Only `active` (non-terminated) enrollments/plans count.

---

## Required Tests

**CRUD & permissions**
- Admin creates a service → 201, all optional fields nullable.
- Manager `POST` / `PUT` / `DELETE` → 403 for each.
- Manager `GET /services/` → 200 (read access is intentional).
- Duplicate `code` → validation error.
- `fee` accepts decimals and round-trips exactly (e.g. `12600.50` stays `12600.50` — the float-vs-Decimal regression test).
- Negative or zero `fee` → validation error.
- Invalid `category` value → validation error.

**Category filtering**
- `?category=installment` returns only installment services — this is what each enrollment wizard calls (`useServices("daily"|"monthly"|"installment"|"online")`).
- No category param → all services.

**Discount pricing**
- `original_fee` greater than `fee` round-trips correctly (the discounted-package display case).
- `original_fee` omitted → null, and the card treats it as non-discounted.

**No registration fee anywhere**
- The Service serializer has no `registration_fee` field, and posting one is ignored rather than stored.
- Enrolling a patient charges exactly the service `fee` — assert the created Payment's amount equals `fee`, with nothing added on top.
- Registering a patient creates **no** Payment at all (registration is free).

**Delete blocked while in use (highest-value tests in this module)**
- Deleting a service with an **active monthly enrollment** → 400, not deleted.
- Deleting a service with an **active installment plan** → 400, not deleted.
- The error payload includes `activeEnrollmentCount` with the correct number, and the message names it.
- Deleting a service whose only enrollments are **terminated** → succeeds.
- Deleting a service with no enrollments at all → succeeds.
- Manager attempting delete → 403 (regardless of enrollment state).

**Deactivate vs delete**
- Deactivating a service with 5 active enrollments → succeeds (unlike delete).
- **Existing enrollments on a deactivated service keep working** — their bills still generate and payments still succeed. This is the point of deactivation; assert it directly.
- Deactivated service is **absent** from `GET /services/?category=...` (the wizard call), so nobody new can enroll.
- Deactivated service **is** present with `?includeInactive=true` for the Admin catalog.
- Manager passing `?includeInactive=true` → inactive services still excluded (Admin-only visibility).
- Reactivating makes it selectable again.
- Manager attempting deactivate/activate → 403.

**Soft delete**
- Deleted service disappears from `/services/` even with `?includeInactive=true`.
- A payment/enrollment referencing a deleted service still resolves the service name — historical receipts must not degrade to "Unknown service".
- Deleted service is excluded from `enrollment-counts`.

**Enrollment counts**
- Service with 2 active monthly enrollments + 1 active installment plan → count of 3.
- Terminated enrollments/plans are **not** counted.
- Service with no enrollments → absent from the map or `0` (pick one, be consistent, and assert it — the frontend renders nothing when the value is undefined).
- Bounded query count (`assertNumQueries`) so a per-service N+1 loop fails the suite.

**Not branch-scoped (deliberate)**
- Managers from different branches both see the identical catalog — assert this explicitly, so a future well-meaning "add branch scoping everywhere" change can't silently break the shared catalog.
