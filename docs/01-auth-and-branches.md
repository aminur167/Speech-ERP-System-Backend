# 01 — Auth & Branches

**Build first.** Everything else depends on the User/Branch models and the branch-scoping permission layer defined here.

Read `docs/00-OVERVIEW.md` first — the branch-isolation rules and money rules there apply throughout and are not repeated in full here.

Frontend source: `src/lib/api/auth.ts`, `src/lib/api/branches.ts`, `src/store/authStore.ts`, `src/hooks/useAuthGuard.ts`.

---

## Models

### User (custom, replaces Django's default)

| Field | Type | Notes |
|---|---|---|
| `email` | EmailField, unique | Used as the login identifier — **not** username |
| `password` | hashed | Django's `set_password()` — never store plaintext |
| `name` | CharField | Frontend shows this as the display name |
| `role` | choices: `admin` \| `manager` | Drives all permissions |
| `branch` | FK → Branch, **nullable** | `null` for admin (they're org-wide), required for manager |
| `is_active` | bool | Deactivating a manager must block their login |

Frontend's `AuthUser` shape (`src/types/domain.ts`): `{ id, name, email, role, branchId }` — note `branchId` is `string | null`, matching admin having no branch.

**Do not use Django's default username-based auth.** The frontend logs in with email + password only.

### Branch

| Field | Type | Notes |
|---|---|---|
| `name` | CharField | e.g. "Dhaka Main Branch" |
| `code` | CharField, unique | e.g. "BR-DHK-001" |
| `status` | choices: `active` \| `inactive` | |
| `address`, `phone` | CharField | |
| `manager_name` | CharField | Display name shown on branch cards |
| `manager_code` | CharField | e.g. "MGR-DHK-001" |
| `manager` | FK → User, nullable | The actual login account for this branch |
| `therapist_count`, `support_count` | PositiveInteger | Staff counts, display only |
| `opened_at` | DateField | |

### ⚠️ Critical deviation from the frontend mock

The mock's `Branch` type carries **`managerEmail` and `managerPassword` as plain fields on the branch object** (see `src/types/domain.ts`), and `BranchDetailView` displays those credentials in a "Manager Login" panel. That is a mock-only convenience and **must not be reproduced server-side**:

- Never store a plaintext password on Branch (or anywhere).
- The manager's credentials live on the linked `User` row, password hashed via `set_password()`.
- `GET /branches/` must **never** return a password field.
- On create/update, accept `managerEmail` + optional `managerPassword` as **write-only** serializer inputs that provision/update the linked User account server-side (this mirrors what `createBranch`/`updateBranch` currently fake via `upsertManagerAccount` in the mock).
- On update, a blank `managerPassword` means "keep existing password" — the mock already behaves this way (`input.managerPassword || existing.managerPassword`), preserve that semantic.

**This requires a small frontend change** when wiring up: `BranchDetailView`'s "Manager Login" panel can keep showing the email, but must stop showing the password. Flag this to the user when the time comes — don't silently break the UI, and don't compromise the backend to preserve it.

---

## Endpoints

### Auth

| Method | Path | Body / Params | Returns |
|---|---|---|---|
| POST | `/auth/login/` | `{ email, password }` | `{ user, accessToken }` |
| GET | `/auth/me/` | — | `AuthUser` — for restoring session on page reload |
| PATCH | `/auth/profile/` | `{ name }` | updated `AuthUser` |
| POST | `/auth/logout/` | — | 204 |

Notes:
- Invalid credentials → the frontend expects a plain message (it reads `error.response.data.detail`), rendering it under the password field. DRF's default `{"detail": "..."}` works as-is.
- **Rate-limit `/auth/login/`** (DRF throttling) — brute-force protection.
- `/auth/me/` matters more than it looks: the frontend's auth state is currently in-memory Zustand (lost on refresh) with `getCurrentUser()` stubbed to return `null`. With a real backend this endpoint restores the session.
- Access tokens short-lived + refresh rotation. Consider httpOnly cookie storage over the current JS-accessible store (see overview).

### Branches

| Method | Path | Notes |
|---|---|---|
| GET | `/branches/` | Admin: all. Manager: only their own branch (they need it for `useCurrentBranchName`) |
| POST | `/branches/` | **Admin only.** Provisions the manager User account too |
| GET | `/branches/{id}/` | Branch-scoped |
| PUT | `/branches/{id}/` | **Admin only.** Blank password = keep existing |
| GET | `/branches/overview/` | **Admin only.** List of `{ branch, patientCount, totalCollected, monthlyRevenue }` |
| GET | `/branches/{id}/overview/` | Same shape, single branch |

The overview endpoints join against Patients and Payments (see `buildBranchOverview` in the mock). Use aggregate queries — do **not** loop branches issuing per-branch queries (N+1). With 4 branches today that's invisible; the discipline matters as the client adds branches and years of payments.

---

## The Branch-Scoping Layer (build this here, reuse everywhere)

This module owns the shared mixin/permission class that every other module depends on. Per `docs/00-OVERVIEW.md`:

- Manager → queryset forced to `request.user.branch`, ignoring any client-supplied branch param.
- Admin → unfiltered, optional `?branch=<id>` to scope.
- Applies to detail/edit endpoints, not just lists — out-of-scope object → 404.
- One implementation, reused by every branch-scoped ViewSet.

Get this right here and the rest of the project inherits it. Get it wrong and every subsequent module inherits the hole.

---

## Required Tests

Per the Definition of Done in the overview — written and passing before moving to `02-patients.md`.

**Auth**
- Login with valid admin credentials → 200, returns user with `role: "admin"` and `branchId: null`.
- Login with valid manager credentials → 200, returns their correct `branchId`.
- Login with wrong password → 401, no token issued.
- Login with non-existent email → 401, and the error message must not reveal whether the email exists.
- Login against an `is_active=False` user → rejected.
- `/auth/me/` without a token → 401.
- `/auth/me/` with a valid token → correct user.
- `/auth/profile/` updates only `name`; attempting to change `role` or `branch` through it must not work.
- Login endpoint throttling actually triggers after repeated failures.

**Branch isolation (the important ones)**
- Manager A `GET /branches/` → sees only their own branch, not Branch B.
- Manager A `GET /branches/{branch_B_id}/` → 404 (not 200, not 403-with-data).
- Manager attempts `POST /branches/` → 403.
- Manager attempts `PUT /branches/{own_id}/` → 403 (branch editing is Admin-only).
- Manager `GET /branches/overview/` → 403.
- Admin `GET /branches/` → sees all branches.
- Admin `GET /branches/{any_id}/overview/` → 200 for every branch.

**Branch/manager provisioning**
- Creating a branch with `managerEmail` + `managerPassword` creates a linked, login-capable User with `role: "manager"` and the correct `branch`.
- The created password is hashed — asserting the stored value ≠ the submitted plaintext.
- **No branch response, on any endpoint, contains a password field.**
- Updating a branch with a blank `managerPassword` leaves the existing password working (old password still logs in).
- Updating a branch with a new `managerPassword` makes the new one work and the old one fail.
- Updating `managerEmail` updates the linked User's login email.
- Duplicate branch `code` → validation error.

**Overview correctness**
- `patientCount` / `totalCollected` / `monthlyRevenue` for a branch count only that branch's records, not other branches'.
- A branch with no patients/payments returns zeros rather than erroring.
