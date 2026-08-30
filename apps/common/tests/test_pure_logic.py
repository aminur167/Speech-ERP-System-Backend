"""
Pure-function tests — no database required.

These cover the calculation logic where subtle bugs actually live: date
arithmetic, money splitting, phone normalisation, reconciliation maths. Every
one of these is a place where a plausible-looking implementation is quietly
wrong for part of the year or part of the input range.

Kept DB-free deliberately so they can run anywhere, including before the
database is configured.
"""

from datetime import date
from decimal import ROUND_DOWN, Decimal

import pytest

from apps.common.sequences import format_code
from apps.common.validators import normalize_phone, validate_bd_phone
from apps.dailyclosing.models import DailyClosing
from apps.enrollments.models import due_date_for_month
from apps.enrollments.services import add_months, month_key, month_label
from apps.patients.models import calculate_age


class TestCalculateAge:
    """
    Naive `year - birth_year` is wrong for anyone whose birthday hasn't
    happened yet — roughly half the patient list on any given day.
    """

    def test_birthday_already_passed_this_year(self):
        assert calculate_age(date(2000, 1, 15), on=date(2026, 3, 1)) == 26

    def test_birthday_still_ahead_this_year(self):
        assert calculate_age(date(2000, 6, 15), on=date(2026, 3, 1)) == 25

    def test_on_the_birthday_itself(self):
        assert calculate_age(date(2000, 5, 10), on=date(2026, 5, 10)) == 26

    def test_day_before_birthday(self):
        assert calculate_age(date(2000, 5, 10), on=date(2026, 5, 9)) == 25

    def test_day_after_birthday(self):
        assert calculate_age(date(2000, 5, 10), on=date(2026, 5, 11)) == 26

    def test_newborn(self):
        assert calculate_age(date(2026, 3, 1), on=date(2026, 3, 1)) == 0

    def test_leap_day_birth_before_feb_29_in_common_year(self):
        # Born 29 Feb; on 28 Feb of a non-leap year the birthday hasn't come.
        assert calculate_age(date(2000, 2, 29), on=date(2026, 2, 28)) == 25

    def test_leap_day_birth_on_march_1(self):
        assert calculate_age(date(2000, 2, 29), on=date(2026, 3, 1)) == 26

    def test_none_date_of_birth(self):
        assert calculate_age(None) is None

    def test_exactly_eighteen_boundary(self):
        """Pins the adult cutoff the guardian rule depends on."""
        assert calculate_age(date(2008, 6, 15), on=date(2026, 6, 15)) == 18
        assert calculate_age(date(2008, 6, 15), on=date(2026, 6, 14)) == 17


class TestPhoneNormalisation:
    """
    The same person entered two ways must become one stored value, or search
    silently splits them into two unfindable records.
    """

    @pytest.mark.parametrize(
        "supplied",
        [
            "01711000001",
            "+8801711000001",
            "8801711000001",
            "+880 1711-000001",
            "01711-000001",
            "01711 000001",
            "  01711000001  ",
            "(017)11000001",
        ],
    )
    def test_all_equivalent_forms_normalise_identically(self, supplied):
        assert normalize_phone(supplied) == "01711000001"

    def test_empty_passes_through(self):
        assert normalize_phone("") == ""

    @pytest.mark.parametrize(
        "valid", ["01311000001", "01411000001", "01511000001", "01611000001",
                  "01711000001", "01811000001", "01911000001"]
    )
    def test_all_real_operator_prefixes_accepted(self, valid):
        validate_bd_phone(valid)  # must not raise

    @pytest.mark.parametrize(
        "invalid",
        [
            "12345",              # too short
            "0171100000",         # 10 digits
            "017110000012",       # 12 digits
            "01211000001",        # prefix 2 isn't a mobile operator
            "01011000001",        # prefix 0
            "abcdefghijk",
            "+15550100000",       # not Bangladeshi
        ],
    )
    def test_malformed_numbers_rejected(self, invalid):
        from django.core.exceptions import ValidationError

        with pytest.raises(ValidationError):
            validate_bd_phone(invalid)


class TestCodeFormatting:
    def test_with_year(self):
        assert format_code("PT", 2026, 42) == "PT-2026-00042"

    def test_without_year(self):
        assert format_code("MAT", None, 7) == "MAT-00007"

    def test_zero_padding_to_five(self):
        assert format_code("RCPT", 2026, 1) == "RCPT-2026-00001"

    def test_does_not_truncate_beyond_five_digits(self):
        """After 99,999 records the code must keep growing, not wrap."""
        assert format_code("PT", 2026, 123456) == "PT-2026-123456"


class TestMonthArithmetic:
    """
    Month stepping must not be done by adding days or by incrementing the
    month field directly — both break at year boundaries and short months.
    """

    def test_add_months_within_year(self):
        assert add_months(date(2026, 3, 1), 2) == date(2026, 5, 1)

    def test_add_months_crosses_year_boundary(self):
        assert add_months(date(2026, 11, 1), 3) == date(2027, 2, 1)

    def test_add_months_december_to_january(self):
        assert add_months(date(2026, 12, 1), 1) == date(2027, 1, 1)

    def test_add_months_full_year(self):
        assert add_months(date(2026, 1, 1), 12) == date(2027, 1, 1)

    def test_add_months_zero_is_identity(self):
        assert add_months(date(2026, 7, 1), 0) == date(2026, 7, 1)

    def test_add_months_from_a_31_day_month(self):
        """Normalising to the 1st avoids 31 Jan + 1 month = invalid 31 Feb."""
        assert add_months(date(2026, 1, 1), 1) == date(2026, 2, 1)

    def test_month_key_zero_pads(self):
        assert month_key(date(2026, 3, 1)) == "2026-03"
        assert month_key(date(2026, 12, 1)) == "2026-12"

    def test_month_label_is_human_readable(self):
        assert month_label(date(2026, 8, 1)) == "August 2026"


class TestDueDate:
    """Due on the 5th of the bill's own month — not 5 days after generation."""

    def test_due_on_the_fifth(self):
        assert due_date_for_month("2026-08") == date(2026, 8, 5)

    def test_due_date_across_year_boundary(self):
        assert due_date_for_month("2027-01") == date(2027, 1, 5)

    def test_february(self):
        assert due_date_for_month("2026-02") == date(2026, 2, 5)


class TestClosingReconciliation:
    """
    Difference and status are computed server-side — accepting them from the
    client would let a screen claim a day balanced when it didn't.
    """

    def test_exact_match(self):
        difference, status = DailyClosing.compute(Decimal("2300.00"), Decimal("2300.00"))
        assert difference == Decimal("0.00")
        assert status == DailyClosing.Status.MATCHED

    def test_over(self):
        difference, status = DailyClosing.compute(Decimal("2300.00"), Decimal("2500.00"))
        assert difference == Decimal("200.00")
        assert status == DailyClosing.Status.OVER

    def test_short(self):
        difference, status = DailyClosing.compute(Decimal("2300.00"), Decimal("2000.00"))
        assert difference == Decimal("-300.00")
        assert status == DailyClosing.Status.SHORT

    def test_one_paisa_short_is_still_short(self):
        """Exactness matters — a rounding fudge here hides real discrepancies."""
        difference, status = DailyClosing.compute(Decimal("2300.00"), Decimal("2299.99"))
        assert difference == Decimal("-0.01")
        assert status == DailyClosing.Status.SHORT

    def test_zero_day_matches(self):
        difference, status = DailyClosing.compute(Decimal("0.00"), Decimal("0.00"))
        assert status == DailyClosing.Status.MATCHED


class TestInstallmentSplitArithmetic:
    """
    The split must sum to exactly the total. Equal division of an amount that
    doesn't divide evenly is where a taka gets silently lost or invented —
    the remainder goes on the final installment.
    """

    @staticmethod
    def split(total: Decimal, parts: int) -> list[Decimal]:
        """Mirrors create_installment_plan — keep the two in step."""
        base = (total / parts).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        remainder = total - (base * parts)
        return [
            base + remainder if index == parts else base
            for index in range(1, parts + 1)
        ]

    def test_even_split(self):
        amounts = self.split(Decimal("6000.00"), 3)
        assert amounts == [Decimal("2000.00")] * 3
        assert sum(amounts) == Decimal("6000.00")

    def test_uneven_split_sums_exactly(self):
        amounts = self.split(Decimal("18500.00"), 3)
        assert sum(amounts) == Decimal("18500.00")

    def test_remainder_lands_on_the_final_part(self):
        """
        Every part except the last is identical, and the last absorbs the
        remainder. Regression guard: with default (banker's) rounding the base
        rounds up, the remainder goes negative, and the final installment ends
        up *smaller* than the rest — charging the patient more up front.
        """
        amounts = self.split(Decimal("18500.00"), 3)

        assert amounts[0] == amounts[1] == Decimal("6166.66")
        assert amounts[-1] == Decimal("6166.68")
        assert amounts[-1] > amounts[0]

    def test_remainder_is_never_negative(self):
        """The property the rounding direction exists to guarantee."""
        for total, parts in [("18500.00", 3), ("100.00", 3), ("999.99", 7), ("0.03", 2)]:
            amounts = self.split(Decimal(total), parts)
            assert amounts[-1] >= amounts[0], f"{total}/{parts} produced a negative remainder"

    @pytest.mark.parametrize(
        "total,parts",
        [
            ("100.00", 3), ("18500.00", 3), ("12600.00", 4),
            ("4000.00", 2), ("999.99", 7), ("1.00", 2), ("0.03", 2),
        ],
    )
    def test_split_always_sums_to_the_total(self, total, parts):
        amounts = self.split(Decimal(total), parts)
        assert sum(amounts) == Decimal(total), f"{total} / {parts} lost or gained money"

    def test_no_float_drift_across_many_parts(self):
        amounts = self.split(Decimal("10000.00"), 12)
        assert sum(amounts) == Decimal("10000.00")
        assert all(isinstance(a, Decimal) for a in amounts)


class TestDecimalMoneyBehaviour:
    """Guards the classic float bug at the arithmetic level."""

    def test_decimal_addition_is_exact(self):
        assert Decimal("0.10") + Decimal("0.20") == Decimal("0.30")

    def test_float_addition_is_not(self):
        """Why money is never a float — this is the bug being avoided."""
        assert 0.1 + 0.2 != 0.3

    def test_summing_many_amounts_stays_exact(self):
        amounts = [Decimal("1234.56")] * 100
        assert sum(amounts) == Decimal("123456.00")

    def test_multiplication_by_quantity_is_exact(self):
        assert Decimal("350.00") * 3 == Decimal("1050.00")
