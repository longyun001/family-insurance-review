# 设计来源与取舍

2026-11-12 阅读 safeCare，固定版本 `d7019509fecd93cd9ec83a15491b87cd915ef764`。这里只借鉴机制，附带脚本为独立 Python 实现，没有复制其 TypeScript 源码，也不依赖 safeCare 的服务。

| 阅读的实现 | 采用的机制 | 本工具的处理 |
|---|---|---|
| [search.ts](https://github.com/iciuu/safeCare/blob/d7019509fecd93cd9ec83a15491b87cd915ef764/lib/search.ts) | 中文二元组 + SQLite FTS5 | 保留来源、物理页码、期间和哈希；按页检索，不猜条款编号 |
| [qa.ts](https://github.com/iciuu/safeCare/blob/d7019509fecd93cd9ec83a15491b87cd915ef764/lib/qa.ts) | 检索后回答、引用反查、证据不足回退原文 | 每个候选事实单独绑定依据；引文存在与结论成立分别检查 |
| [enrich.ts](https://github.com/iciuu/safeCare/blob/d7019509fecd93cd9ec83a15491b87cd915ef764/lib/ai/enrich.ts) | 引文规范化与匹配 | 不采用 0.9 相似度放行。数字、否定词、标点和比较符号变化会使严格引用校验失败 |
| [elements.ts](https://github.com/iciuu/safeCare/blob/d7019509fecd93cd9ec83a15491b87cd915ef764/lib/elements.ts) / [review.ts](https://github.com/iciuu/safeCare/blob/d7019509fecd93cd9ec83a15491b87cd915ef764/lib/review.ts) | 差异保留、待复核队列与修改留痕 | 初始事实不自动确认；原始事实不可覆盖，更正引用重新验证，修改记录版本冲突检查 |
| [estimate.ts](https://github.com/iciuu/safeCare/blob/d7019509fecd93cd9ec83a15491b87cd915ef764/lib/estimate.ts) / [stacking.ts](https://github.com/iciuu/safeCare/blob/d7019509fecd93cd9ec83a15491b87cd915ef764/lib/stacking.ts) | 区分费用补偿与定额给付、列示条件 | 只采用核对原则，没有引入通用赔付计算器；不沿用缺医院范围时的默认值、内置比例或按名称选择责任的启发式 |

重要边界：严格引文匹配只验证文字及出处，不做语义蕴含判断。真实引用也可能被误解。原文的表格列关系、脚注、主附险边界和特别约定仍须核读；提取文本质量不足时回看原始页面。

SQLite 索引、通用检索脚本和技能说明中没有任何外部模型请求。需要摘要或问答时，由当前助手利用检索出的资料完成。

## 家庭保险整理工具的补充

2026-11-13 阅读 [Zuomeng00/family-insurance-manager](https://github.com/Zuomeng00/family-insurance-manager)，固定版本 `574d4d40e7e20c3753b00f7206ae16b21833fe44`。排期脚本为独立实现，没有复制该仓库代码，也没有部署其 Web 应用或通知服务。

| 阅读的实现 | 采用的机制 | 本技能的处理 |
|---|---|---|
| [calc.py](https://github.com/Zuomeng00/family-insurance-manager/blob/574d4d40e7e20c3753b00f7206ae16b21833fe44/app/calc.py) | 原始日期锚点、月末/闰年处理、按期枚举 | 支持年/半年/季/月/单次，按完整查询窗口枚举；金额以整数分计算 |
| [stats_service.py](https://github.com/Zuomeng00/family-insurance-manager/blob/574d4d40e7e20c3753b00f7206ae16b21833fe44/app/services/stats_service.py) | 区分年化保费与按期现金流，多被保人保单避免重复统计 | 分开输出应缴参考额、待核实参考额和实付记录小计，付款按实际日期归月，币种分别合计 |
| [notification_service.py](https://github.com/Zuomeng00/family-insurance-manager/blob/574d4d40e7e20c3753b00f7206ae16b21833fe44/app/services/notification_service.py) | 保障到期和缴费到期分别收集、合并成一份清单 | 只生成本地近期清单；没有创建自动推送或复制 Webhook 发送逻辑 |
| [attachment_service.py](https://github.com/Zuomeng00/family-insurance-manager/blob/574d4d40e7e20c3753b00f7206ae16b21833fe44/app/services/attachment_service.py) | 文件类型/数量/大小校验、成组提交 | 当前技能直接查阅已有原件，无上传服务；不引入与任务无关的登录、附件删除或服务器配置功能 |

没有沿用的推断：

- 原项目 calc.py 的 paid_periods_override 为 None 时，将应到期数作为已缴期数；本技能始终把缺收据的期次保留为 unknown，不据日期推断实际扣款。
- 原项目的累计缴费可由已缴期数乘当前每期保费派生；本技能只按明确逐笔记录累计，避免保费变化、提前缴和历史快照混入实际流水。
- 保障期位置不等于实时效力，缺起止日期不能自动视为已生效；终身没有到期日，短期一次缴清也不自动代表未来续保价格已知。
- 原项目近 60 天函数有 4 次循环保护；本技能的 365 天或更长窗口按总期数完整枚举，不复用短窗口的循环上限。

## WorkBuddy 技能副本的整合

2026-11-13 对比本地 WorkBuddy 的 `family-insurance-review` 技能副本，通用文件的实质变化为 PDF 提取模块。吸收 PyMuPDF 优先、pypdf 回退的后端支持，并加入显式后端选择、实际提取器版本和回退原因，保留本技能现有的检索、严格引文核验与复核审计接口。

没有沿用直接删除 U+FFFD、U+FFFF、Unicode 私用区和控制符的清理方式：这些字符可能代表未知正文，删除会把「2�0」拼成「20」。整合后原始逐页文本不改写，异常类型和数量按页记录，继续提示目视核读；字体警告也不固定写成已解决。后端改善提取完整性与合同事实得到核实是两件事。

旧数据库格式与事实记录保持兼容。更换后端时另建索引验证，不覆盖已有引文和复核历史；实际使用过的后端在输出中可查。技能包只包含通用代码和说明，未并入 WorkBuddy 私人证据库、记忆台账或不同编号体系的保单记录。
