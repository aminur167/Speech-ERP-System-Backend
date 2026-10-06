"""
Query budgets: how many database round trips each endpoint may cost.

Every statement is a network round trip to a hosted database, so the number
of them -- not the SQL itself -- is most of what a request costs on the
clinic's server. Two things are pinned here:

1. **No endpoint's query count grows with its data.** Each read is measured
   with a few rows and again with several times as many; the count must be
   identical. That is the property that catches an N+1 (a query per row)
   the day someone adds one, without needing a benchmark.

2. **Each endpoint stays inside a budget** -- the number it costs today. A
   change that makes an endpoint chattier fails here and has to say so;
   one that makes it cheaper should lower the number.

The counts are production-like: reads are plain, and writes run in real
transactions (`transaction=True`) so BEGIN/COMMIT count as they do live,
rather than the savepoints a test's own wrapping transaction would add.
"""

from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.enrollments import services as enrollment_services
from apps.expenses import services as expense_services
from apps.notifications.models import Notification
from apps.payments import services as payment_services
from apps.patients import attendance
from apps.services.models import Service
from apps.staff import services as staff_services


class Seeder:
    """Creates more of everything on demand, with unique codes."""

    def __init__(self, *, manager, branch, patient_factory, service_factory,
                 material_factory, staff_member_factory, admin_user):
        self.manager, self.branch, self.admin = manager, branch, admin_user
        self.patient_factory, self.material_factory = patient_factory, material_factory
        self.staff_member_factory = staff_member_factory
        self.n = 0
        self.monthly = service_factory(name="Mon", code="M1", fee=Decimal("5000"),
                                       admission_fee=Decimal("3000"))
        self.plan_service = service_factory(
            name="Ins", code="I1", category=Service.Category.INSTALLMENT, fee=Decimal("18000"))
        self.online = service_factory(
            name="Onl", code="O9", category=Service.Category.ONLINE, fee=Decimal("1000"))
        self.pats, self.enrollments, self.staff = [], [], []

    def grow(self, count):
        for _ in range(count):
            self.n += 1
            n = self.n
            patient = self.patient_factory(
                name=f"Patient {n}", phone=f"017{n:08d}", patient_code=f"PT-QB-{n:05d}")
            self.pats.append(patient)
            enrollment = enrollment_services.create_monthly_enrollment(
                actor=self.manager, branch=self.branch, patient=patient, service=self.monthly)
            self.enrollments.append(enrollment)
            if n % 2:
                enrollment_services.collect_bill_payment(
                    actor=self.manager, branch=self.branch, bill=enrollment.bills.first(),
                    method="cash")
            payment, _ = payment_services.create_payment(
                actor=self.manager, branch=self.branch, patient=patient,
                amount=Decimal("800"), method="bkash" if n % 3 else "cash", category="daily")
            if n % 3 == 0:
                payment_services.request_refund(
                    actor=self.manager, payment=payment, amount=Decimal("50"), reason="x")
            self.material_factory(branch=self.branch, quantity=100)
            self.staff.append(self.staff_member_factory(name=f"Staff {n}"))
            expense_services.create_expense(
                actor=self.manager, branch=self.branch,
                data={"category": "supplies", "amount": Decimal("9000" if n % 2 else "100"),
                      "description": f"x{n}", "paid_to": "y", "payment_method": "cash"})
            # Marked by the manager, so each marked row has someone to name.
            attendance.mark(actor=self.manager, patient=patient, kind="monthly",
                            status="present" if n % 2 else "informed_absence")
            Notification.objects.create(recipient=self.admin, title="t", message="m")
            if n % 3 == 0:
                enrollment_services.create_booking(
                    actor=self.manager, branch=self.branch, patient=patient,
                    service=self.online, booking_date=timezone.localdate(),
                    booking_time=f"{10 + n % 8:02d}:00", method="cash")
        # one installment plan per growth step
        plan_patient = self.patient_factory(
            name=f"Plan {self.n}", phone=f"018{self.n:08d}", patient_code=f"PT-QB-P{self.n:04d}")
        enrollment_services.create_installment_plan(
            actor=self.manager, branch=self.branch, patient=plan_patient,
            service=self.plan_service, number_of_installments=3)
        staff_services.request_salary_payment(
            actor=self.manager, staff=self.staff[-1],
            month=timezone.localdate().strftime("%Y-%m"))


WINDOW = {"dateFrom": "2026-01-01", "dateTo": "2026-12-31"}

# (label, client, url, params, budget)
#
# `budget` counts everything after the login lookup, including COUNT(*) for
# paginated lists. A paginated list is user + count + page = 3 at the floor.
READS = [
    ("patients list", "manager", "/api/patients/", {}, 3),
    ("patients search", "manager", "/api/patients/", {"search": "Patient"}, 3),
    ("patient directory", "manager", "/api/patients/directory/", {}, 6),
    ("directory summary", "manager", "/api/patients/directory/summary/", {}, 2),
    ("attendance roster", "manager", "/api/patients/attendance/roster/", {"kind": "monthly"}, 7),
    ("services list", "manager", "/api/services/", {"includePending": "true"}, 4),
    ("enrollment counts", "manager", "/api/services/enrollment-counts/", {}, 3),
    ("materials list", "manager", "/api/materials/", {}, 3),
    ("materials summary", "manager", "/api/materials/summary/", {}, 2),
    ("staff list", "manager", "/api/staff/", {}, 3),
    ("staff summary", "manager", "/api/staff/summary/", {}, 4),
    ("payments list", "manager", "/api/payments/", {}, 3),
    ("transactions list", "manager", "/api/transactions/", {}, 3),
    ("transactions summary", "manager", "/api/transactions/summary/", {}, 4),
    ("dashboard metrics", "manager", "/api/transactions/dashboard-metrics/", {}, 2),
    ("branch summary", "manager", "/api/transactions/branch-summary/", WINDOW, 8),
    ("branch ledger", "manager", "/api/transactions/branch-summary/daily/", WINDOW, 4),
    ("branch activity", "manager", "/api/transactions/branch-summary/activity/", WINDOW, 8),
    ("expenses list", "manager", "/api/expenses/", {}, 3),
    ("expenses summary", "manager", "/api/expenses/summary/", {}, 2),
    ("due payments", "manager", "/api/due-payments/", {}, 3),
    ("due summary", "manager", "/api/due-payments/summary/", {}, 3),
    ("refund requests", "manager", "/api/refund-requests/", {}, 4),
    ("salary payments", "manager", "/api/staff/salary-payments/", {}, 3),
    ("notifications", "admin", "/api/notifications/", {}, 3),
    ("monthly enrollments", "manager", "/api/enrollments/monthly/", {}, 4),
    ("installment plans", "manager", "/api/enrollments/installments/", {}, 4),
    ("bookings", "manager", "/api/enrollments/bookings/", {}, 3),
    ("branches overview", "admin", "/api/branches/overview/", {}, 4),
    ("audit logs", "admin", "/api/audit-logs/", {}, 3),
]


@pytest.mark.django_db
def test_no_read_grows_with_its_data_and_each_stays_in_budget(
    manager_client, admin_client, manager, admin_user, branch, patient_factory,
    service_factory, material_factory, staff_member_factory,
):
    seeder = Seeder(
        manager=manager, branch=branch, patient_factory=patient_factory,
        service_factory=service_factory, material_factory=material_factory,
        staff_member_factory=staff_member_factory, admin_user=admin_user,
    )
    clients = {"manager": manager_client, "admin": admin_client}

    def measure():
        counts = {}
        for label, who, url, params, _budget in READS:
            clients[who].get(url, params)  # warm anything cached per process
            with CaptureQueriesContext(connection) as ctx:
                response = clients[who].get(url, params)
            assert response.status_code == 200, (label, response.status_code)
            counts[label] = len(ctx.captured_queries)
        return counts

    seeder.grow(3)
    small = measure()
    seeder.grow(12)
    large = measure()

    growing = {label: (small[label], large[label]) for label in small if large[label] != small[label]}
    assert not growing, f"query count grew with the data (N+1?): {growing}"

    over = {
        label: (large[label], budget)
        for label, _who, _url, _params, budget in READS
        if large[label] > budget
    }
    assert not over, f"over budget (cost, budget): {over}"


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_writes_stay_in_budget(
    manager_client, admin_client, manager, admin_user, branch, patient_factory,
    service_factory, material_factory, staff_member_factory,
):
    """
    Counted in real transactions, BEGIN and COMMIT included, after the login
    lookup. The floor for a write is: its own inserts and updates, the audit
    entry, a sequence draw where it issues a number, and the locks that keep
    two managers from doing the same thing at once -- those are correctness,
    not overhead.
    """
    seeder = Seeder(
        manager=manager, branch=branch, patient_factory=patient_factory,
        service_factory=service_factory, material_factory=material_factory,
        staff_member_factory=staff_member_factory, admin_user=admin_user,
    )
    seeder.grow(6)
    mats = [material_factory(branch=branch, quantity=500) for _ in range(3)]
    online = service_factory(name="Onl", code="O1", category=Service.Category.ONLINE,
                             fee=Decimal("1000"))
    payer = seeder.pats[2]
    fresh = [patient_factory(name=f"Fresh {i}", phone=f"0199000{i:04d}", patient_code=f"PT-QB-F{i}")
             for i in range(3)]
    plan = enrollment_services.create_installment_plan(
        actor=manager, branch=branch, patient=fresh[2], service=seeder.plan_service,
        number_of_installments=3)
    first_installment = plan.installments.order_by("index").first()
    bill_enrollment = seeder.enrollments[1]  # an even one: its first bill is still open
    bill = bill_enrollment.bills.first()
    paid_for_refund, _ = payment_services.create_payment(
        actor=manager, branch=branch, patient=payer, amount=Decimal("500"), method="cash",
        category="daily")
    pending_refund = payment_services.request_refund(
        actor=manager, payment=paid_for_refund, amount=Decimal("50"), reason="x")

    post = manager_client.post
    cases = [
        ("create patient", 7, lambda: post("/api/patients/", {
            "name": "Zed", "date_of_birth": "1990-01-01", "gender": "male",
            "phone": "01911112222", "address": "x"}, format="json")),
        ("update patient", 7, lambda: manager_client.patch(
            f"/api/patients/{payer.pk}/", {"notes": "hi"}, format="json")),
        ("daily payment", 7, lambda: post("/api/payments/", {
            "patient": payer.pk, "amount": "800", "method": "cash", "category": "daily"},
            format="json")),
        ("enroll monthly + admit fee", 14, lambda: post("/api/enrollments/monthly/", {
            "patient": fresh[0].pk, "service": seeder.monthly.pk, "method": "cash"}, format="json")),
        ("pay a bill", 12, lambda: post(
            f"/api/enrollments/monthly/{bill_enrollment.pk}/bills/{bill.pk}/pay/",
            {"method": "cash"}, format="json")),
        ("enroll installment", 11, lambda: post("/api/enrollments/installments/", {
            "patient": fresh[1].pk, "service": seeder.plan_service.pk,
            "numberOfInstallments": 3}, format="json")),
        ("pay an installment", 10, lambda: post(
            f"/api/enrollments/installments/{plan.pk}/installments/{first_installment.pk}/pay/",
            {"method": "cash"}, format="json")),
        ("sell 3 materials", 12, lambda: post("/api/materials/sell/", {
            "patient": payer.pk, "method": "cash",
            "items": [{"material": m.pk, "quantity": 1} for m in mats]}, format="json")),
        ("adjust stock", 8, lambda: post(f"/api/materials/{mats[0].pk}/adjust-stock/", {
            "type": "in", "quantity": 5, "note": "n"}, format="json")),
        ("expense (needs approval)", 8, lambda: post("/api/expenses/", {
            "category": "supplies", "amount": "9000", "description": "x", "paid_to": "y",
            "payment_method": "cash"}, format="json")),
        ("void a payment", 6, lambda: post(
            f"/api/payments/{paid_for_refund.pk}/void/", {"reason": "x"}, format="json")),
        ("mark attendance", 5, lambda: post(f"/api/patients/{payer.pk}/attendance/", {
            "serviceKind": "monthly", "status": "present"}, format="json")),
        ("staff check-in", 6, lambda: post(f"/api/staff/{seeder.staff[0].pk}/check-in/", {},
                                           format="json")),
        ("book a session", 11, lambda: post("/api/enrollments/bookings/", {
            "patient": payer.pk, "service": online.pk, "date": str(timezone.localdate()),
            "time": "11:00", "method": "cash"}, format="json")),
        ("(admin) approve a refund", 12, lambda: admin_client.post(
            f"/api/refund-requests/{pending_refund.pk}/approve/", {"billAction": "reopen"},
            format="json")),
    ]

    over = {}
    for label, budget, call in cases:
        with CaptureQueriesContext(connection) as ctx:
            response = call()
        assert response.status_code in (200, 201), (label, response.status_code, response.content[:200])
        # Everything except the login lookup, which every request shares.
        cost = len(ctx.captured_queries) - 1
        if cost > budget:
            over[label] = (cost, budget)
    assert not over, f"over budget (cost, budget): {over}"
