#!/usr/bin/env python3
"""Local payment/expiry planning; recorded payments are never inferred from dates."""
from __future__ import annotations

import argparse
import calendar
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import sys

VERSION = "1.1.0"
MONTHS = {"monthly": 1, "quarterly": 3, "half_yearly": 6, "yearly": 12, "once": 0}
STATES = {"unverified", "active", "paid_off", "surrendered", "lapsed", "terminated"}
STOPPED = {"surrendered", "lapsed", "terminated"}
RECORD_STATES = {"paid", "unpaid", "waived"}


def iso(value, label, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValueError(f"invalid_date:{label}")
    result = date.fromisoformat(value)
    if result.isoformat() != value:
        raise ValueError(f"invalid_date:{label}")
    return result


def cents(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError("invalid_amount")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid_amount") from exc
    if not number.is_finite() or number < 0 or number > Decimal("1000000000000"):
        raise ValueError("invalid_amount")
    result = number * 100
    if result != result.to_integral_value():
        raise ValueError("amount_requires_at_most_two_decimal_places")
    return int(result)


def display_money(value):
    return "待补" if value is None else f"{Decimal(value) / 100:,.2f}"


def add_months(anchor, months):
    """Always offset from the original anchor, including month-end and leap years."""
    year, month_index = divmod(anchor.year * 12 + anchor.month - 1 + months, 12)
    month = month_index + 1
    return date(year, month, min(anchor.day, calendar.monthrange(year, month)[1]))


def periods(payment):
    anchor = iso(payment.get("anchor"), "payment.anchor", optional=True)
    count = payment.get("periods")
    frequency = payment.get("frequency")
    if count is not None and (type(count) is not int or not 1 <= count <= 2400):
        raise ValueError("invalid_payment_periods")
    if frequency is not None and frequency not in MONTHS:
        raise ValueError("invalid_payment_frequency")
    if frequency == "once" and count not in (None, 1):
        raise ValueError("one_time_payment_requires_one_period")
    if not anchor or count is None or frequency is None:
        return []
    result = []
    for number in range(1, count + 1):
        result.append((number, add_months(anchor, MONTHS[frequency] * (number - 1))))
    return result


def normalized_policy(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"].strip():
        raise ValueError("policy_id_required")
    policy_id = raw["id"]
    if raw.get("scope") not in ("current", "historical", "reference"):
        raise ValueError(f"explicit_scope_required:{policy_id}")
    currency = raw.get("currency", "CNY")
    if not isinstance(currency, str) or not re.fullmatch("[A-Z]{3}", currency):
        raise ValueError(f"invalid_currency:{policy_id}")
    status = raw.get("recorded_status", "unverified")
    if status not in STATES:
        raise ValueError(f"invalid_policy_status:{policy_id}")
    payment = raw.get("payment") or {}
    coverage = raw.get("coverage") or {"kind": "unknown"}
    if not isinstance(payment, dict) or not isinstance(coverage, dict):
        raise ValueError(f"invalid_policy_structure:{policy_id}")
    due_dates = periods(payment)
    amount = cents(payment.get("amount"))
    kind = coverage.get("kind", "unknown")
    if kind not in ("fixed", "whole_life", "unknown"):
        raise ValueError(f"invalid_coverage_kind:{policy_id}")
    start = iso(coverage.get("start"), "coverage.start", optional=True)
    end = iso(coverage.get("end"), "coverage.end", optional=True)
    if kind == "whole_life" and end is not None:
        raise ValueError(f"whole_life_cannot_have_expiry:{policy_id}")
    if kind == "unknown" and end is not None:
        raise ValueError(f"expiry_requires_fixed_coverage:{policy_id}")
    if start and end and end < start:
        raise ValueError(f"invalid_coverage_range:{policy_id}")
    records, record_ids = {}, set()
    raw_records = payment.get("records", [])
    if not isinstance(raw_records, list):
        raise ValueError("records_must_be_array")
    for record in raw_records:
        if not isinstance(record, dict):
            raise ValueError("record_must_be_object")
        number = record.get("period")
        if type(number) is not int or number < 1 or number > 2400:
            raise ValueError("invalid_record_period")
        if payment.get("periods") is not None and number > payment["periods"]:
            raise ValueError("record_exceeds_payment_periods")
        if number in records:
            raise ValueError("duplicate_record_period")
        record_id = record.get("id")
        if not isinstance(record_id, str) or not record_id.strip() or record_id in record_ids:
            raise ValueError("unique_record_id_required")
        record_ids.add(record_id)
        if record.get("status") not in RECORD_STATES:
            raise ValueError("invalid_record_status")
        if not isinstance(record.get("evidence"), str) or not record["evidence"].strip():
            raise ValueError("payment_record_requires_evidence")
        observed = iso(record.get("observed_on"), "record.observed_on")
        paid_on = iso(record.get("paid_on"), "record.paid_on", optional=True)
        actual = cents(record.get("amount"))
        if record["status"] == "paid":
            if paid_on is None or actual is None or paid_on > observed:
                raise ValueError("paid_record_requires_amount_and_observed_payment_date")
        elif paid_on is not None or actual is not None:
            raise ValueError("nonpaid_record_cannot_contain_paid_amount_or_date")
        records[number] = {**record, "observed_on": observed, "paid_on": paid_on,
                           "actual_cents": actual}
    if status == "paid_off" and any(r["status"] == "unpaid" for r in records.values()):
        raise ValueError(f"paid_off_conflicts_with_unpaid_record:{policy_id}")
    return {
        "id": policy_id, "title": raw.get("title") or policy_id, "currency": currency,
        "scope": raw["scope"], "recorded_status": status, "payment": payment,
        "coverage": coverage, "kind": kind, "start": start, "end": end,
        "due_dates": due_dates, "amount_cents": amount, "records": records,
        "source_ids": raw.get("source_ids", []), "notes": raw.get("notes", ""),
    }


def policy_term(policy, as_of):
    if policy["start"] and as_of < policy["start"]:
        return "not_started"
    if policy["end"] and as_of > policy["end"]:
        return "recorded_term_ended"
    if policy["kind"] == "whole_life" or policy["end"]:
        return "within_recorded_term"
    return "unknown"


def make_plan(raw, as_of, start=None, days=365, remind_days=60):
    if type(days) is not int or not 1 <= days <= 3660:
        raise ValueError("days_must_be_between_1_and_3660")
    if type(remind_days) is not int or not 0 <= remind_days <= 3660:
        raise ValueError("invalid_reminder_window")
    as_of = iso(as_of, "as_of") if isinstance(as_of, str) else as_of
    start = iso(start, "start") if isinstance(start, str) else (start or as_of)
    end, reminder_end = start + timedelta(days=days - 1), as_of + timedelta(days=remind_days)
    policies = raw.get("policies") if isinstance(raw, dict) else raw
    if not isinstance(policies, list):
        raise ValueError("policies_array_required")
    normalized = [normalized_policy(p) for p in policies]
    ids = [p["id"] for p in normalized]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate_policy_ids")
    all_record_ids = [r["id"] for p in normalized for r in p["records"].values()]
    if len(all_record_ids) != len(set(all_record_ids)):
        raise ValueError("duplicate_payment_record_ids")
    events, receipts, reminders, attention, summaries = [], [], [], [], []
    for policy in normalized:
        if policy["scope"] != "current":
            continue
        policy_id = policy["id"]
        known_records = {n: r for n, r in policy["records"].items() if r["observed_on"] <= as_of}
        # Explicit receipts only; membership count never multiplies a payment.
        for number, record in known_records.items():
            if record["status"] == "paid" and start <= record["paid_on"] <= end:
                receipts.append({"record_id": record["id"], "policy_id": policy_id,
                                 "period": number, "date": record["paid_on"].isoformat(),
                                 "amount_cents": record["actual_cents"], "currency": policy["currency"],
                                 "evidence": record["evidence"]})
        current = policy["recorded_status"] not in STOPPED
        unknown_past = []
        unpaid_past = []
        paid_count = sum(r["status"] == "paid" for r in known_records.values())
        summaries.append({
            "policy_id": policy_id, "recorded_term_status": policy_term(policy, as_of),
            "live_effect_verified": False, "recorded_paid_periods": paid_count,
            "notes": policy["notes"], "source_ids": policy["source_ids"],
            "payment_history_complete": len(known_records) == len(policy["due_dates"]) if policy["due_dates"] else False,
            "ignored_future_records": len(policy["records"]) - len(known_records),
        })
        if not current:
            continue
        settled = policy["recorded_status"] == "paid_off"
        if not policy["due_dates"] and not settled:
            attention.append({"policy_id": policy_id, "issue": "payment_schedule_incomplete"})
        if policy["amount_cents"] is None and not settled:
            attention.append({"policy_id": policy_id, "issue": "payment_amount_unknown"})
        for number, due in ([] if settled else policy["due_dates"]):
            record = known_records.get(number)
            state = record["status"] + "_recorded" if record else "unknown"
            item = {
                "event_id": f"{policy_id}:payment:{number}", "kind": "payment_due",
                "policy_id": policy_id, "title": policy["title"], "period": number,
                "date": due.isoformat(), "days_from_as_of": (due - as_of).days,
                "amount_cents": policy["amount_cents"], "currency": policy["currency"],
                "payment_state": state,
                "record_observed_on": record["observed_on"].isoformat() if record else None,
                "date_basis": policy["payment"].get("date_basis", "unverified_reference"),
                "amount_basis": policy["payment"].get("amount_basis", "unverified_reference"),
                "source_ids": policy["source_ids"],
            }
            if due < as_of:
                if state == "unknown":
                    unknown_past.append(due)
                elif state == "unpaid_recorded":
                    unpaid_past.append(due)
            if start <= due <= end:
                events.append(item)
            # A closed reminder window includes today; today is not overdue.
            if as_of <= due <= reminder_end and state not in ("paid_recorded", "waived_recorded"):
                reminders.append(item)
        if unknown_past:
            attention.append({"policy_id": policy_id, "issue": "past_payment_status_unknown",
                              "period_count": len(unknown_past), "earliest_due": min(unknown_past).isoformat(),
                              "not_an_overdue_finding": True})
        if unpaid_past:
            attention.append({"policy_id": policy_id, "issue": "past_due_with_unpaid_record",
                              "period_count": len(unpaid_past), "earliest_due": min(unpaid_past).isoformat(),
                              "record_dates": sorted({r["observed_on"].isoformat()
                                                      for r in known_records.values() if r["status"] == "unpaid"}),
                              "requires_current_status_check": True})
        if policy["kind"] == "fixed" and policy["end"]:
            expiry = {
                "event_id": f"{policy_id}:expiry:{policy['end'].isoformat()}", "kind": "coverage_end",
                "policy_id": policy_id, "title": policy["title"], "date": policy["end"].isoformat(),
                "days_from_as_of": (policy["end"] - as_of).days,
                "date_basis": policy["coverage"].get("end_basis", "unverified_reference"),
                "renewal": policy["coverage"].get("renewal", "unknown"),
                "renewal_quote_cents": None, "source_ids": policy["source_ids"],
            }
            if start <= policy["end"] <= end:
                events.append(expiry)
            if as_of <= policy["end"] <= reminder_end:
                reminders.append(expiry)
            if policy["end"] < as_of:
                attention.append({"policy_id": policy_id, "issue": "recorded_term_ended_check_new_policy",
                                  "date": policy["end"].isoformat()})
        elif policy["kind"] != "whole_life":
            attention.append({"policy_id": policy_id, "issue": "coverage_end_unknown"})
    events.sort(key=lambda e: (e["date"], e["policy_id"], e["kind"]))
    reminders.sort(key=lambda e: (e["date"], e["policy_id"], e["kind"]))
    monthly = defaultdict(lambda: {
        "scheduled_reference_cents": 0, "pending_reference_cents": 0, "recorded_paid_cents": 0,
        "scheduled_count": 0, "payment_status_unknown_count": 0,
        "scheduled_amount_unknown_count": 0, "pending_amount_unknown_count": 0, "recorded_receipt_count": 0,
    })
    for event in events:
        if event["kind"] != "payment_due":
            continue
        item = monthly[(event["date"][:7], event["currency"])]
        item["scheduled_count"] += 1
        item["payment_status_unknown_count"] += event["payment_state"] == "unknown"
        if event["amount_cents"] is None:
            item["scheduled_amount_unknown_count"] += 1
        else:
            item["scheduled_reference_cents"] += event["amount_cents"]
        if event["payment_state"] not in ("paid_recorded", "waived_recorded"):
            if event["amount_cents"] is None:
                item["pending_amount_unknown_count"] += 1
            else:
                item["pending_reference_cents"] += event["amount_cents"]
    for receipt in receipts:
        item = monthly[(receipt["date"][:7], receipt["currency"])]
        item["recorded_paid_cents"] += receipt["amount_cents"]
        item["recorded_receipt_count"] += 1
    monthly_rows = [{"month": month, "currency": currency, **values}
                    for (month, currency), values in sorted(monthly.items())]
    totals = defaultdict(lambda: {key: 0 for key in (
        "scheduled_reference_cents", "pending_reference_cents", "recorded_paid_cents",
        "scheduled_count", "payment_status_unknown_count", "scheduled_amount_unknown_count",
        "pending_amount_unknown_count", "recorded_receipt_count")})
    for row in monthly_rows:
        for key in totals[row["currency"]]:
            totals[row["currency"]][key] += row[key]
    return {
        "version": VERSION, "as_of": as_of.isoformat(), "window_start": start.isoformat(),
        "window_end": end.isoformat(), "reminder_end": reminder_end.isoformat(),
        "data_snapshot": raw.get("data_snapshot") if isinstance(raw, dict) else None,
        "totals_by_currency": dict(totals), "monthly": monthly_rows,
        "events": events, "reminders": reminders, "attention": attention, "receipts": receipts,
        "policy_summaries": summaries, "input_policy_count": len(normalized),
        "current_policy_count": len(summaries), "notification_scheduled": False,
        "caveats": [
            "Dates describe supplied contract/payment schedules, not live policy validity.",
            "No payment record means unknown; zero recorded receipts is not zero actual payments.",
            "Reference premiums are not paid totals or future renewal quotes.",
            "No external messages or scheduled notifications are created.",
        ],
    }


def markdown(plan):
    labels = {"unknown": "待核实", "paid_recorded": "有已缴记录", "unpaid_recorded": "有未缴记录",
              "waived_recorded": "有豁免记录"}
    bases = {"anniversary_reference": "合同周年参考日", "contract_start_reference": "合同起期参考日",
             "unverified_reference": "待核实的参考日期", "confirmed": "已核读日期",
             "explicit": "资料明示止期", "derived": "按期间推算止期", "unknown": "依据待补"}

    def safe(value):
        return str(value).replace("|", "／").replace("\n", " ").replace("\r", " ")

    lines = [
        "# 家庭保险缴费与到期参考清单", "",
        f"核对日：{plan['as_of']}；排期窗口：{plan['window_start']} 至 {plan['window_end']}。",
        f"资料快照：{plan.get('data_snapshot') or '未注明'}。", "",
        "金额是资料中的参考保费；已过应缴日而缺少记录的期次保留为待核实。"
        "记录实付小计不等于全部实际支出。续保新价格留空，保障期内也不证明实时有效。", "",
        "## 近期缴费与保障到期", "",
        f"提醒范围：核对日至 {plan['reminder_end']}（含首尾日）。", "",
        "| 日期 | 保障编号 | 事项 | 参考金额 | 状态/依据 |",
        "|---|---|---|---:|---|",
    ]
    for item in plan["reminders"]:
        if item["kind"] == "payment_due":
            money = f"{item['currency']} {display_money(item['amount_cents'])}"
            note = f"{labels[item['payment_state']]}；{bases.get(item['date_basis'], item['date_basis'])}"
            action = f"第 {item['period']} 期缴费"
        else:
            money, action = "待续保报价", "保障期满，核对续保/新保单"
            note = bases.get(item["date_basis"], item["date_basis"])
        lines.append(f"| {item['date']} | {safe(item['policy_id'])} | {action} | {money} | {safe(note)} |")
    if not plan["reminders"]:
        lines.append("| — | — | 本窗口未生成已知到期项 | — | 资料不全项目另列 |")
    lines.extend(["", "## 分月现金流参考", "",
                  "| 月份 | 币种 | 已知排期金额 | 其中未有已缴/豁免记录的参考额 | 有记录的实付小计 | 实付记录笔数 | 金额待补期数 |",
                  "|---|---|---:|---:|---:|---:|---:|"])
    for row in plan["monthly"]:
        actual = display_money(row["recorded_paid_cents"]) if row["recorded_receipt_count"] else "无实付记录"
        lines.append(
            f"| {row['month']} | {row['currency']} | {display_money(row['scheduled_reference_cents'])} | "
            f"{display_money(row['pending_reference_cents'])} | {actual} | "
            f"{row['recorded_receipt_count']} | {row['scheduled_amount_unknown_count']} |")
    lines.extend(["", "## 排期窗口内保障期满", "",
                  "| 日期 | 保障编号 | 日期依据 | 后续保费 |",
                  "|---|---|---|---|"])
    expiring = [e for e in plan["events"] if e["kind"] == "coverage_end"]
    for item in expiring:
        basis = bases.get(item["date_basis"], item["date_basis"])
        lines.append(f"| {item['date']} | {safe(item['policy_id'])} | {safe(basis)} | 待续保/新投保报价 |")
    if not expiring:
        lines.append("| — | — | 本窗口未生成已知期满项 | 未推算续保价 |")
    lines.extend(["", "## 待补与核实", ""])
    issue_names = {
        "payment_schedule_incomplete": "缴费日期、频次或期数待补",
        "payment_amount_unknown": "保费待补，未按零元计费",
        "past_payment_status_unknown": "既往期次缺少逐笔缴费记录；未判定为逾期",
        "past_due_with_unpaid_record": "存在过应缴日的未缴记录，需按记录日期核对当前情况",
        "coverage_end_unknown": "保障止期待补",
        "recorded_term_ended_check_new_policy": "登记期间已结束，需核对续保或新保单",
    }
    for item in plan["attention"]:
        number = f"（{item['period_count']} 期）" if "period_count" in item else ""
        lines.append(f"- {safe(item['policy_id'])}：{issue_names[item['issue']]}{number}。")
    if not plan["attention"]:
        lines.append("- 本次未发现格式完整性待补项；仍需按来源核对实时情况。")
    notes = [p for p in plan["policy_summaries"] if p["notes"]]
    if notes:
        lines.extend(["", "## 来源备注", ""])
        for policy in notes:
            lines.append(f"- {safe(policy['policy_id'])}：{safe(policy['notes'])}")
    lines.extend(["", "本文件是本地参考排期，没有创建定时通知或发送外部消息。", ""])
    return "\n".join(lines)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--start")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--remind-days", type=int, default=60)
    parser.add_argument("--out")
    parser.add_argument("--markdown")
    args = parser.parse_args()
    try:
        input_path = Path(args.input).resolve()
        outputs = [Path(p).resolve() for p in (args.out, args.markdown) if p]
        if input_path in outputs or len(set(outputs)) != len(outputs):
            raise ValueError("output_paths_must_not_overwrite_input_or_each_other")
        raw = json.loads(input_path.read_text(encoding="utf-8-sig"))
        plan = make_plan(raw, args.as_of, args.start, args.days, args.remind_days)
        encoded = json.dumps(plan, ensure_ascii=False, indent=2, allow_nan=False)
        if args.out:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(encoded, encoding="utf-8")
        if args.markdown:
            Path(args.markdown).parent.mkdir(parents=True, exist_ok=True)
            Path(args.markdown).write_text(markdown(plan), encoding="utf-8")
        if outputs:
            print(json.dumps({"status": "written", "as_of": plan["as_of"], "outputs": list(map(str, outputs)),
                              "reminders": len(plan["reminders"]), "events": len(plan["events"]),
                              "notification_scheduled": False}, ensure_ascii=False))
        else:
            print(encoded)
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
