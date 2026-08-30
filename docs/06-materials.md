# 06 — Materials & Inventory

Depends on `01`, `02`, `04`.

Read `docs/00-OVERVIEW.md` first for the cross-cutting rules.

Frontend source: `src/lib/api/materials.ts`, `src/components/materials/MaterialListView.tsx`, `SellMaterialsView.tsx`.

---

## Models

### Material — **branch-scoped** (unlike Services)

Physical stock doesn't move between locations, so each branch owns its own material rows and quantities. This is the deliberate opposite of the shared Service catalog in `03`.

| Field | Type | Notes |
|---|---|---|
| `name` | CharField | |
| `code` | CharField, unique | `MAT-00001` — race-safe generation |
| `image_url` | URLField, optional | Cloudinary delivery URL — see below |
| `image_public_id` | CharField, optional | 🆕 Cloudinary identifier, needed to replace/delete the asset |
| `unit` | choices | `piece`\|`box`\|`packet`\|`set`\|`bottle`\|`other` |
| `quantity` | PositiveInteger | Current stock on hand |
| `unit_cost` | **Decimal** | What the clinic paid — drives stock *value* |
| `selling_price` | **Decimal** | What the patient is charged — allows markup |
| `reorder_level` | PositiveInteger | At or below → flagged "Low Stock" |
| `branch` | FK → Branch | |
| `created_at` | DateTimeField | |
| `is_deleted` | bool | Soft delete — movement history references it |

`unit_cost` and `selling_price` are deliberately separate: stock value uses cost, sales revenue uses selling price. Don't collapse them into one field.

### ✅ CONFIRMED: images go to Cloudinary

Material images are stored on **Cloudinary**; the database keeps only the URL (plus the `public_id`, see below). The mock's base64 data URLs are not carried over — they bloat rows, slow backups, and can't be CDN-cached.

**Store two fields, not one:**
- `image_url` — the delivery URL rendered by the UI
- `image_public_id` — Cloudinary's identifier

The `public_id` matters: without it there's no reliable way to delete or replace the asset later, and the account slowly fills with orphaned images nobody can identify. Deriving it by parsing the URL is fragile — store it.

**Upload approach — decide before building:**
- **Signed direct upload from the browser** (recommended): backend issues a short-lived signature, the file goes straight to Cloudinary, and the frontend posts back the resulting URL + `public_id`. Keeps large uploads off the Django server entirely.
- **Proxy through the backend**: simpler to reason about, but every image consumes server memory/bandwidth and ties up a worker.

Either way the backend must **validate what it accepts** — the API must never blindly trust a client-supplied `image_url`. Restrict to the clinic's own Cloudinary account/folder, or verify the `public_id` against Cloudinary before saving; otherwise anyone can point a material's image at an arbitrary external URL.

**Also required:**
- Keep the Cloudinary API secret server-side only — never in `NEXT_PUBLIC_*` env vars.
- Constrain uploads: file type (jpg/png/webp), max size, and a max dimension transformation so a 12MP phone photo doesn't get served as-is to the POS screen.
- On material image replacement or hard cleanup, delete the old Cloudinary asset using its `public_id`. On **soft delete**, keep the image (history may still reference it).
- Image is optional — a material with no image must render fine (POS shows a placeholder).

**Frontend follow-up:** replace the current base64/data-URL upload in the material form with the Cloudinary flow. Not built yet.

### MaterialMovement — the audit trail for stock

Every quantity change writes a row. Never mutate `quantity` without one.

`material` FK, `type` (`in`|`out`), `quantity`, `note` (optional), `branch` FK, `created_by` FK → User, `created_at`

Sales auto-write a movement per line item with note `"Sold — Payment {receiptNumber}"` (mirrors the mock), linking inventory movement back to the financial record.

---

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/materials/` | Branch-scoped |
| POST | `/materials/` | |
| PUT | `/materials/{id}/` | |
| DELETE | `/materials/{id}/` | Soft delete |
| GET | `/materials/summary/` | `{ totalItems, totalStockValue, lowStockCount }` |
| POST | `/materials/{id}/adjust-stock/` | `{ type, quantity, note }` |
| GET | `/materials/{id}/movements/` | Newest first |
| POST | `/materials/sell/` | POS checkout — see below |

Summary math (from `getMaterialsSummary`): `totalStockValue = Σ(quantity × unit_cost)` (cost, not selling price); `lowStockCount` = items where `quantity <= reorder_level`.

### Stock adjustment
Atomic, with `select_for_update()` on the material row. `in` adds, `out` subtracts. **Reject if the result would go negative** — the mock returns `"Not enough stock for this adjustment."` Always write a MaterialMovement in the same transaction.

### `POST /materials/sell/` — the POS checkout

The most complex single operation in this module. Input: `{ items: [{ materialId, quantity, unitPrice }], patientId, method, idempotencyKey }`.

All of this in **one `transaction.atomic()`**:
1. `select_for_update()` every material in the cart (lock before validating)
2. Validate stock for **every** line first — if any line is short, fail the whole sale (all-or-nothing; no partial fulfilment). Error names the offending item: `"Not enough stock for \"{name}\"."`
3. Compute the total **server-side** from stored `selling_price` — see the warning below
4. Create **one** `Payment` with `category: "material_sale"`, honoring the idempotency key
5. Deduct each material's quantity
6. Write one MaterialMovement per line, noting the receipt number

Returns the updated materials plus the Payment (the frontend renders a receipt immediately).

> ⚠️ **Never trust the client-supplied `unitPrice`.** The mock computes the total from prices posted by the browser, which would let a tampered request buy a ৳2,500 kit for ৳1. Recompute from the database. Accept the client value only as an optional consistency check that rejects mismatches — and note this is a genuine security fix over the mock, not an optional refinement.

Empty cart → reject (`"Cart is empty."`).

---

## Required Tests

**CRUD & branch isolation**
- Materials created against the authenticated manager's branch, ignoring any posted branch.
- Manager A's `/materials/` excludes Branch B's items.
- Manager A `GET`/`PUT`/`DELETE` on a Branch B material → 404.
- **Two branches can hold same-named materials with independent quantities** — adjusting one must not affect the other (the core branch-scoping test for this module).
- `code` unique; concurrent creates produce unique codes.
- Soft-deleted materials leave their movement history resolvable.

**Images (Cloudinary)**
- Creating a material **without** an image → succeeds, `image_url` null.
- Creating with a valid Cloudinary URL + `public_id` → both stored.
- **An `image_url` pointing outside the clinic's Cloudinary account is rejected** (the injection test — a client must not be able to set an arbitrary external URL).
- Replacing an image stores the new `public_id` and deletes the old Cloudinary asset.
- **Soft-deleting a material does not delete its Cloudinary asset** (history may still reference it).
- Cloudinary API secret is not exposed in any API response or client-readable config.
- Oversized/wrong-type uploads rejected per the configured limits.

**Summary math**
- `totalStockValue` uses `unit_cost`, **not** `selling_price` (easy and costly to get backwards).
- `lowStockCount` counts `quantity <= reorder_level` — test exactly-at-threshold, below, and above.
- Branch-scoped: another branch's stock never contributes.
- Empty branch → zeros, not an error.

**Stock adjustment**
- `in` increases, `out` decreases; each writes a MaterialMovement with the correct type and acting user.
- Adjustment that would go negative → rejected, **quantity unchanged**, no movement written.
- Adjusting to exactly zero → allowed.
- Concurrent adjustments on one material don't lose an update (lost-update test).

**Sell flow (highest-value tests here)**
- Single-line sale: stock deducted, one Payment created with `category: "material_sale"`, one movement written.
- Multi-line sale: **one** Payment for the combined total, one movement **per line**.
- **Total computed from stored `selling_price`** — post a tampered low `unitPrice` and assert the charge matches the database price, not the submitted one.
- Insufficient stock on **any** line → entire sale rejected, **no stock deducted from any line**, no Payment created (the all-or-nothing test).
- Empty cart → rejected.
- **Concurrent sales of the last unit → exactly one succeeds**, the other is rejected; stock never goes negative.
- Idempotency key replay → one Payment, one deduction, identical response (a retried sale must not deduct stock twice).
- **Atomicity:** force a failure after the Payment is created but before deduction completes — assert full rollback, no orphaned Payment, no partial stock change.
- Movement note references the created Payment's receipt number.
- Sold materials' revenue appears in reporting as `material_sale` category.
