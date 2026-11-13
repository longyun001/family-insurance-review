#!/usr/bin/env python3
"""Fictional payment scenarios; does not read or alter private policy records."""
from datetime import date
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import json

import insurance_schedule as schedule


def policy(**changes):
    result = {
        "id": "P001", "scope": "current", "title": "虚构保单", "currency": "CNY",
        "payment": {"anchor": "2026-01-31", "frequency": "monthly", "periods": 12,
                    "amount": "100.10", "records": []},
        "coverage": {"kind": "fixed", "start": "2026-01-31", "end": "2027-01-30",
                     "end_basis": "explicit"},
    }
    result.update(changes)
    return result


def paid(period=1, paid_on="2026-01-31", amount="100.10", record_id="R001"):
    return {"id": record_id, "period": period, "status": "paid", "amount": amount,
            "paid_on": paid_on, "observed_on": paid_on, "evidence": "虚构收据"}


def payments(plan):
    return [e for e in plan["events"] if e["kind"] == "payment_due"]


class ScheduleTests(unittest.TestCase):
    def test_month_end_does_not_drift(self):
        dates = schedule.periods(policy()["payment"])
        self.assertEqual([d for _, d in dates[:3]],
                         [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)])

    def test_leap_anniversary_recovers_in_next_leap_year(self):
        p = {"anchor": "2024-02-29", "frequency": "yearly", "periods": 5}
        dates = schedule.periods(p)
        self.assertEqual(dates[1][1], date(2025, 2, 28))
        self.assertEqual(dates[4][1], date(2028, 2, 29))

    def test_quarterly_and_half_yearly(self):
        self.assertEqual(schedule.periods({"anchor": "2026-11-30", "frequency": "quarterly", "periods": 3})[-1][1],
                         date(2027, 5, 30))
        self.assertEqual(schedule.periods({"anchor": "2026-08-31", "frequency": "half_yearly", "periods": 2})[-1][1],
                         date(2027, 2, 28))

    def test_period_count_stops_future_payments(self):
        p = policy()
        p["payment"]["periods"] = 2
        self.assertEqual(len(payments(schedule.make_plan([p], "2026-01-01", days=365))), 2)

    def test_long_horizon_not_silently_limited_to_three_months(self):
        result = schedule.make_plan([policy()], "2026-01-01", days=365)
        self.assertEqual(len(payments(result)), 12)
        self.assertEqual(result["totals_by_currency"]["CNY"]["scheduled_reference_cents"], 120120)

    def test_one_time_policy_does_not_create_renewal_premium(self):
        p = policy()
        p["payment"].update(frequency="once", periods=1)
        result = schedule.make_plan([p], "2026-02-01", days=730)
        self.assertEqual(payments(result), [])
        self.assertIsNone(result["events"][0]["renewal_quote_cents"])

    def test_payment_due_today_not_past_or_paid(self):
        result = schedule.make_plan([policy()], "2026-01-31", days=1, remind_days=0)
        self.assertEqual(payments(result)[0]["payment_state"], "unknown")
        self.assertEqual(payments(result)[0]["days_from_as_of"], 0)
        self.assertEqual(result["policy_summaries"][0]["recorded_paid_periods"], 0)
        self.assertFalse(any(i["issue"].startswith("past_") for i in result["attention"]))

    def test_past_date_never_infers_paid_or_unpaid(self):
        result = schedule.make_plan([policy()], "2026-03-01")
        past = next(i for i in result["attention"] if i["issue"] == "past_payment_status_unknown")
        self.assertEqual(past["period_count"], 2)
        self.assertTrue(past["not_an_overdue_finding"])
        self.assertEqual(result["policy_summaries"][0]["recorded_paid_periods"], 0)
        self.assertEqual(result["receipts"], [])

    def test_recorded_receipt_uses_actual_amount_and_payment_date(self):
        p = policy()
        p["payment"]["records"] = [paid(paid_on="2026-02-02", amount="99.99")]
        result = schedule.make_plan([p], "2026-02-03", start="2026-01-01", days=60)
        self.assertEqual(result["receipts"][0]["amount_cents"], 9999)
        self.assertEqual(next(r for r in result["monthly"] if r["month"] == "2026-02")["recorded_paid_cents"], 9999)
        self.assertEqual(payments(result)[0]["payment_state"], "paid_recorded")

    def test_advance_payment_not_counted_again_at_due_date(self):
        p = policy()
        p["payment"]["records"] = [paid(2, "2026-01-20")]
        result = schedule.make_plan([p], "2026-02-01", days=28)
        self.assertEqual(result["receipts"], [])
        self.assertEqual(result["totals_by_currency"]["CNY"]["pending_reference_cents"], 0)
        self.assertFalse(any(item.get("period") == 2 for item in result["reminders"]))

    def test_future_record_not_used_in_historical_report(self):
        p = policy()
        p["payment"]["records"] = [paid(1, "2026-02-02")]
        result = schedule.make_plan([p], "2026-01-31", days=1)
        self.assertEqual(payments(result)[0]["payment_state"], "unknown")
        self.assertEqual(result["policy_summaries"][0]["ignored_future_records"], 1)

    def test_unpaid_record_retains_its_observation_date(self):
        p = policy()
        p["payment"]["records"] = [{
            "id": "R1", "period": 1, "status": "unpaid", "observed_on": "2026-02-01",
            "evidence": "虚构核实记录",
        }]
        result = schedule.make_plan([p], "2026-02-10")
        item = next(i for i in result["attention"] if i["issue"] == "past_due_with_unpaid_record")
        self.assertTrue(item["requires_current_status_check"])
        self.assertEqual(item["record_dates"], ["2026-02-01"])

    def test_unknown_amount_not_assumed_free(self):
        p = policy()
        p["payment"]["amount"] = None
        result = schedule.make_plan([p], "2026-01-31", days=1)
        self.assertIsNone(payments(result)[0]["amount_cents"])
        self.assertEqual(result["totals_by_currency"]["CNY"]["scheduled_amount_unknown_count"], 1)
        self.assertIn("payment_amount_unknown", [i["issue"] for i in result["attention"]])

    def test_legitimate_zero_amount_is_distinct_from_unknown(self):
        p = policy()
        p["payment"]["amount"] = "0.00"
        result = schedule.make_plan([p], "2026-01-31", days=1)
        self.assertEqual(payments(result)[0]["amount_cents"], 0)
        self.assertEqual(result["totals_by_currency"]["CNY"]["scheduled_amount_unknown_count"], 0)

    def test_missing_schedule_and_expiry_remain_unknown(self):
        p = policy(payment={}, coverage={"kind": "unknown"})
        result = schedule.make_plan([p], "2026-01-31")
        self.assertEqual(result["events"], [])
        self.assertEqual({i["issue"] for i in result["attention"]},
                         {"payment_schedule_incomplete", "payment_amount_unknown", "coverage_end_unknown"})

    def test_whole_life_has_payments_but_no_expiry(self):
        result = schedule.make_plan([policy(coverage={"kind": "whole_life", "start": "2026-01-31"})],
                                    "2026-01-31")
        self.assertEqual(len(payments(result)), 12)
        self.assertFalse(any(e["kind"] == "coverage_end" for e in result["events"]))
        self.assertFalse(result["policy_summaries"][0]["live_effect_verified"])

    def test_future_coverage_not_marked_already_in_term(self):
        result = schedule.make_plan([policy()], "2026-01-01")
        self.assertEqual(result["policy_summaries"][0]["recorded_term_status"], "not_started")

    def test_paid_off_suppresses_payment_without_inventing_receipts(self):
        result = schedule.make_plan([policy(recorded_status="paid_off")], "2026-01-31", days=365)
        self.assertEqual(payments(result), [])
        self.assertEqual(result["receipts"], [])
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["events"][0]["kind"], "coverage_end")

    def test_surrendered_policy_has_no_future_alerts(self):
        result = schedule.make_plan([policy(recorded_status="surrendered")], "2026-01-31")
        self.assertEqual(result["events"], [])
        self.assertEqual(result["reminders"], [])

    def test_history_and_reference_excluded_from_current_totals(self):
        a, b, c = policy(), policy(id="P002", scope="historical"), policy(id="P003", scope="reference")
        result = schedule.make_plan([a, b, c], "2026-01-31", days=1)
        self.assertEqual(result["current_policy_count"], 1)
        self.assertEqual(result["totals_by_currency"]["CNY"]["scheduled_reference_cents"], 10010)

    def test_multiple_insured_people_dont_multiply_premium(self):
        p = policy(insured_ids=["M1", "M2", "M3"])
        result = schedule.make_plan([p], "2026-01-31", days=1)
        self.assertEqual(result["totals_by_currency"]["CNY"]["scheduled_reference_cents"], 10010)

    def test_currency_totals_separate(self):
        result = schedule.make_plan([policy(), policy(id="P002", currency="USD")], "2026-01-31", days=1)
        self.assertEqual(set(result["totals_by_currency"]), {"CNY", "USD"})
        self.assertEqual(result["totals_by_currency"]["USD"]["scheduled_reference_cents"], 10010)

    def test_reminder_window_includes_last_day(self):
        p = policy()
        result = schedule.make_plan([p], "2026-01-01", remind_days=30)
        self.assertEqual(result["reminders"][0]["date"], "2026-01-31")
        self.assertEqual(schedule.make_plan([p], "2026-01-01", remind_days=29)["reminders"], [])

    def test_expiry_today_and_derived_basis_preserved(self):
        p = policy()
        p["coverage"]["end_basis"] = "derived"
        result = schedule.make_plan([p], "2027-01-30", days=1, remind_days=0)
        self.assertEqual(result["reminders"][0]["kind"], "coverage_end")
        self.assertEqual(result["reminders"][0]["date_basis"], "derived")
        self.assertEqual(result["reminders"][0]["days_from_as_of"], 0)

    def test_records_require_evidence_and_cannot_duplicate_period(self):
        p = policy()
        p["payment"]["records"] = [paid(), paid(record_id="R2")]
        with self.assertRaisesRegex(ValueError, "duplicate_record_period"):
            schedule.make_plan([p], "2026-02-01")
        p["payment"]["records"] = [{**paid(), "evidence": ""}]
        with self.assertRaisesRegex(ValueError, "requires_evidence"):
            schedule.make_plan([p], "2026-02-01")

    def test_duplicate_policy_or_receipt_ids_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate_policy_ids"):
            schedule.make_plan([policy(), policy()], "2026-01-01")
        p, q = policy(), policy(id="P002")
        p["payment"]["records"], q["payment"]["records"] = [paid()], [paid()]
        with self.assertRaisesRegex(ValueError, "duplicate_payment_record_ids"):
            schedule.make_plan([p, q], "2026-02-01")

    def test_invalid_money_periods_and_dates_rejected(self):
        for value in ("NaN", "Infinity", "-1", "1.005", True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                schedule.cents(value)
        for count in (0, -1, 1.5, True, 2401):
            p = policy()
            p["payment"]["periods"] = count
            with self.subTest(count=count), self.assertRaises(ValueError):
                schedule.make_plan([p], "2026-01-31")
        with self.assertRaises(ValueError):
            schedule.make_plan([policy()], "2026-02-30")

    def test_no_implicit_period_scope_or_renewal(self):
        p = policy()
        p["payment"]["periods"] = None
        self.assertEqual(payments(schedule.make_plan([p], "2026-01-31")), [])
        del p["scope"]
        with self.assertRaisesRegex(ValueError, "explicit_scope"):
            schedule.make_plan([p], "2026-01-31")

    def test_cannot_claim_paid_before_observed_payment_date(self):
        p = policy()
        p["payment"]["records"] = [{**paid(paid_on="2026-02-02"), "observed_on": "2026-02-01"}]
        with self.assertRaisesRegex(ValueError, "observed_payment_date"):
            schedule.make_plan([p], "2026-02-10")

    def test_contradictory_paid_off_record_rejected(self):
        p = policy(recorded_status="paid_off")
        p["payment"]["records"] = [{"id": "R1", "period": 1, "status": "unpaid",
                                    "observed_on": "2026-02-01", "evidence": "虚构核实"}]
        with self.assertRaisesRegex(ValueError, "paid_off_conflicts"):
            schedule.make_plan([p], "2026-02-01")

    def test_markdown_does_not_imply_actual_paid_total_or_live_status(self):
        plan = schedule.make_plan([policy()], "2026-02-01")
        text = schedule.markdown(plan)
        self.assertIn("不等于全部实际支出", text)
        self.assertIn("未判定为逾期", text)
        self.assertIn("无实付记录", text)
        self.assertIn("排期窗口内保障期满", text)
        self.assertFalse(plan["notification_scheduled"])

    def test_cli_outputs_and_refuses_overwriting_input(self):
        parent = Path(tempfile.gettempdir()).resolve()
        with tempfile.TemporaryDirectory(prefix="insurance-schedule-test-", dir=parent) as folder:
            root = Path(folder).resolve()
            self.assertEqual(root.parent, parent)
            source = root / "输入.json"
            encoded = json.dumps({"policies": [policy()]}, ensure_ascii=False)
            source.write_text(encoded, encoding="utf-8")
            output, report = root / "plan.json", root / "清单.md"
            command = [sys.executable, str(Path(schedule.__file__).resolve()), "--input", str(source),
                       "--as-of", "2026-01-31", "--out", str(output), "--markdown", str(report)]
            subprocess.run(command, check=True, capture_output=True, text=True, encoding="utf-8")
            self.assertTrue(output.exists())
            self.assertTrue(report.exists())
            rejected = subprocess.run(command[:-4] + ["--out", str(source)], capture_output=True,
                                      text=True, encoding="utf-8")
            self.assertNotEqual(rejected.returncode, 0)
            self.assertEqual(source.read_text(encoding="utf-8"), encoded)


if __name__ == "__main__":
    unittest.main(verbosity=2)
