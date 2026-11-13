# 家庭保险核对 · family-insurance-review

一个在本地整理保险资料的 Codex 技能，附带可单独运行的 Python 工具。它把保单检索、原文引用、候选事实复核和缴费排期连起来，帮助回答「依据在哪一页」「这项记录核过没有」「哪些缴费信息还需要补齐」。

仓库只包含通用代码、说明和虚构示例。运行产生的证据库、真实保单及台账应保存在自己的本地项目中。

## 可以做什么

| 功能 | 用途 |
| --- | --- |
| 本地证据检索 | 为 PDF、TXT、Markdown 建立 SQLite FTS5 索引，支持中文二元组检索，保留来源编号和 PDF 物理页码 |
| 原文引用校验 | 检查引文是否连续出自指定来源和页面，保留数字、否定词、标点与比较符号，检测原文件是否变化 |
| 候选事实与复核记录 | 导入有引用依据的事实，记录确认、更正、忽略、重新核对及版本历史，列出同一条件和期间的字段差异 |
| PDF 提取诊断 | 默认优先 PyMuPDF，缺少或失败时回退 pypdf；记录实际后端、回退原因和逐页异常字符，保留原始文本 |
| 缴费与保障期满排期 | 支持年、半年、季、月及单次缴费，按原始日期锚点处理月末和闰年，生成本地 JSON / Markdown 清单 |
| 分月现金流 | 分开列示应缴参考额、缺少缴费记录的参考额和实际付款记录小计；金额用整数分，币种分别汇总 |

脚本没有网络请求，也不需要模型 API、向量数据库或服务器。使用 Codex 分析资料时，仍应遵循所用助手和运行环境的数据处理规则；本地脚本的运行方式不代表助手整体离线。

## 安装与环境

- Python 3.10 或更新版本。
- 证据检索需要当前 Python 自带的 SQLite 支持 FTS5。
- PDF 提取需要至少一个 PDF 后端；处理 TXT / Markdown、复核记录及生成排期只需要标准库。

克隆仓库后进入目录：

```sh
git clone https://github.com/longyun001/family-insurance-review.git
cd family-insurance-review
```

需要读取 PDF 时，把依赖安装到实际执行脚本的 Python 中：

```sh
python -m pip install -r requirements.txt
```

`requirements.txt` 固定了本次验证使用的 PyMuPDF 和 pypdf 版本。也可以只安装其中一个后端；默认 `auto` 优先 PyMuPDF。指定 `--pdf-backend pymupdf` 或 `--pdf-backend pypdf` 时，失败会直接报错。

作为 Codex 技能使用时，把这个仓库目录放在 `$CODEX_HOME/skills/family-insurance-review` 下；默认目录为 `~/.codex/skills/family-insurance-review`。重新打开 Codex 后，可在任务中使用：

> 使用 $family-insurance-review 核对我提供的保险资料，先检索原文，列出引用、候选差异和待补信息。

仅使用命令行工具时，无需安装为技能。

## 用虚构资料试运行

在仓库根目录执行下列命令。`examples/` 中的条款、金额、日期和编号全部为虚构数据。

先建立索引，查找交费期间并核对原文：

```sh
python scripts/insurance_evidence.py index --db local/evidence.sqlite --manifest examples/sources.json
python scripts/insurance_evidence.py search --db local/evidence.sqlite --query "交费期间" --policy P001
python scripts/insurance_evidence.py page --db local/evidence.sqlite --source D01 --page 1
python scripts/insurance_evidence.py verify --db local/evidence.sqlite --source D01 --page 1 --quote "交费期间为20年，交费方式为年交，每期保险费为1000.10元。"
```

`examples/policy.md` 是 UTF-8 文本，按第 1 页处理。实际 PDF 使用从 1 开始的物理页码。清单里的相对文件路径以清单所在目录为基准。

再导入一项带引用的候选事实：

```sh
python scripts/insurance_evidence.py add-facts --db local/evidence.sqlite --file examples/facts.json
python scripts/insurance_evidence.py facts --db local/evidence.sqlite --policy P001
python scripts/insurance_evidence.py conflicts --db local/evidence.sqlite --policy P001 --scope all
python scripts/insurance_evidence.py check --db local/evidence.sqlite
```

新增事实的状态为 `needs_review`。核读后才能执行 `review`，并如实填写操作主体和原因。引文校验通过只证明文字在来源中存在，结论是否成立仍需结合适用条件和上下文检查。

生成一份缴费排期：

```sh
python scripts/insurance_schedule.py --input examples/schedule-input.json --as-of 2026-11-13 --days 365 --remind-days 60 --out local/plan.json --markdown local/plan.md
```

示例的核对日固定为 `2026-11-13`，实际使用请替换为需要核对的日期。示例保单没有实缴凭证，历史期次会保留为未知；排期窗口内的 `2027-01-31` 只是一笔 `1000.10 CNY` 的应缴参考。终身保障不会产生保障期满日。

`local/` 已加入 `.gitignore`。重复体验时使用新的数据库文件名，可获得独立的示例记录。

## 用于自己的资料

1. 在自己的项目中保存保单原件，按 `examples/sources.json` 编写来源清单，明确区分当期、历史及参考资料。
2. 建立本地证据库，读取检索命中页及必要的前后页，再验证准备引用的原文。
3. 按 `examples/facts.json` 记录候选事实，逐项核读，保留更正理由和复核历史。
4. 用排期输入记录合同日期、每期金额及明确的付款凭证；缺资料时保留未知，不能由日期推断已经扣款。

完整格式和命令见 [证据工具说明](references/commands.md) 与 [缴费排期说明](references/schedules.md)。

更换 PDF 后端时，已有数据库会继续复用相同文件哈希对应的页面。需要重新提取时，使用新的数据库路径，保留旧库中的事实及复核记录；新库不会自动继承确认状态。

## 使用边界

- PDF 文本提取不包含 OCR。扫描页、表格、字体缺字及低质量文本需要回看原页；异常字符不会被删除后拼成看似完整的引文。
- `current` 表示本次指定的当期资料，`confirmed` 表示一项事实已经核读；二者均不证明保单实时有效或保险公司已认可。
- 费用补偿、定额给付和住院津贴分别核对，不能直接合计成一个「总保障」。赔付计算需要具体条款及完整条件。
- 排期使用合同或台账的参考信息，缺少收据保留 `unknown`；参考保费不等同未来续保报价。
- `reminders` 是本地报告中的近期清单，运行脚本不会自动建立定时任务或发送通知。

## 文件结构

```text
family-insurance-review/
├── SKILL.md
├── agents/openai.yaml
├── scripts/
│   ├── insurance_evidence.py
│   ├── insurance_schedule.py
│   ├── test_insurance_evidence.py
│   ├── test_insurance_pdf.py
│   └── test_insurance_schedule.py
├── references/
│   ├── commands.md
│   ├── schedules.md
│   └── design-notes.md
├── examples/
│   ├── policy.md
│   ├── sources.json
│   ├── facts.json
│   └── schedule-input.json
├── requirements.txt
└── README.md
```

## 验证

```sh
python -m unittest discover -s scripts -p "test_insurance_*.py" -v
```

当前包含 77 项回归测试：证据工具 25 项、排期工具 32 项、PDF 后端 20 项。完整验证应安装 `requirements.txt` 中的两个 PDF 后端；未安装相应后端时，部分真实 PDF 测试会跳过。测试使用临时目录和虚构资料。

## 设计来源

本技能参考了 [safeCare](https://github.com/iciuu/safeCare) 的检索与证据复核机制、[家庭保险整理工具](https://github.com/Zuomeng00/family-insurance-manager) 的日期与现金流机制，并整合了本地 WorkBuddy 技能副本的 PDF 后端改进。附带 Python 工具为独立实现；来源版本、采用的机制及没有沿用的推断见 [设计说明](references/design-notes.md)。
