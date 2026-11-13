# 缴费与保障到期排期

工具：本技能下 scripts/insurance_schedule.py。Python 3.10+，只使用标准库。金额输入是每期保费，不是年化金额；输出金额字段以整数分表示。数据、输出文件都放在当前用户项目里。

## 输入

```json
{
  "data_snapshot": "2026-11-05",
  "policies": [
    {
      "id": "P001",
      "title": "示例长期保障",
      "scope": "current",
      "currency": "CNY",
      "recorded_status": "unverified",
      "source_ids": ["D01"],
      "payment": {
        "anchor": "2021-01-31",
        "frequency": "yearly",
        "periods": 20,
        "amount": "1000.10",
        "date_basis": "anniversary_reference",
        "amount_basis": "ledger_reference",
        "records": []
      },
      "coverage": {
        "kind": "whole_life",
        "start": "2021-01-31",
        "end": null
      }
    }
  ]
}
```

- scope 必须显式为 current、historical 或 reference，只有 current 参与本次排期。id 必须唯一；历史保单版本不要作为第二份当期保障重复输入。
- recorded_status 支持 unverified、active、paid_off、surrendered、lapsed、terminated。它是资料中登记的状态，不由日期自动判定实时效力。paid_off 停止缴费排期，仍可列保障期满；退保、失效、终止不生成未来提醒。
- frequency 支持 yearly、half_yearly、quarterly、monthly、once。periods 是合同总期数；once 必须为 1。未知字段保留 null；缺日期、频次、期数时列待补，不推算完整缴费表。
- 固定期间使用 coverage.kind=fixed，填写原始资料所载的 end。止期由期间推算时 end_basis=derived；合同明确写明时 end_basis=explicit。保障期限未知用 kind=unknown，end=null。
- 缴费锚点通常是合同周年参考日期。只有逐单确认后才把 date_basis 改为 confirmed。金额来源写在 amount_basis，参考保费不能变成未来续保报价。

明确的已缴记录示例：

```json
{
  "id": "R001",
  "period": 6,
  "status": "paid",
  "amount": "1000.10",
  "paid_on": "2026-01-25",
  "observed_on": "2026-01-26",
  "evidence": "用户明确确认或实际扣费凭证的来源编号/路径"
}
```

放入该保单 payment.records 数组。同一期只放一项当前记录，记录 id 在整个输入中唯一，避免一笔付款重复统计。金额及日期用实际凭证；付款金额与参考保费有差异时保留实际值，不用当前保费乘期数替代。早于实缴日期的核实记录会被拒绝；核实日期晚于排期核对日的记录不用于该次历史报表。

未缴或豁免记录使用 status=unpaid 或 waived，仍需 id、period、observed_on、evidence，不填写 amount 和 paid_on。未缴记录的历史核实日会保留，不能因此断言今天仍未缴。更正收据时保留原始资料/上版输入，或沿用证据工具的复核留痕机制。

## 命令

```text
python TOOL --input schedule-input.json --as-of 2026-11-13 --days 365 --remind-days 60 --out plan.json --markdown plan.md
```

TOOL 替换为 scripts/insurance_schedule.py 的绝对路径。--as-of 必填，不能默用执行机器的日期。--start 可指定现金流窗口起日，默认核对日；--days 表示包含起日的总天数，提醒窗口独立为核对日至加 remind-days，含首尾日。

## 输出口径

- events：指定窗口内的缴费日、保障期满日，分别标识 kind。
- reminders：近期参考清单，已登记缴费/豁免的期次排除；它是本地报告，不会定时推送。
- monthly / totals_by_currency：应缴参考额、尚缺已缴/豁免记录的参考额、实付记录小计与记录笔数、金额待补期数。金额不全时只能把已知金额看作小计。
- receipts：以实际付款日归入现金流月份，提前付款不在应缴月重复计为支出。
- attention：缺金额、缺期间、既往缴费状态未知、已有历史未缴记录等核实任务；未知不被标为逾期。
- policy_summaries：记录期间位置、已登记缴费期数和未来记录排除数。live_effect_verified 始终为 false，日期运算不验证保单实时效力。

回归测试：python scripts/test_insurance_schedule.py。测试使用虚构资料，不更改现有台账或证据库。
