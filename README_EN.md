# Journal Quality Assessment 0.2.0

[中文](README.md) · [Commands/recovery](references/public_workflow.md) · [Method](references/public_method.md) · [Synthetic output example](references/output_example.md)

An Agent Skill and standalone Python CLI for title/abstract-based article ratings,
equal-article journal means, balanced pairing, Agent-file or DeepSeek execution,
auditable receipts, recovery, cost reservations and CSV/JSON export. It implements
an engineered REF-inspired adaptation, not official REF ratings or a full replication
of the cited paper. No BT_score or Bayesian adjustment. Python3.10+, standard library only.

## Install and try without credentials

Extract the clean Skill ZIP. Run `python scripts/public_cli.py doctor`, then
`python scripts/install.py --project /path/to/project` (or `--user`). Refresh your
supported Agent and invoke `$journal-quality-assessment`. Python/filesystem tools
must actually be available; installing a plugin does not add those capabilities.
Alternatively install the supplied wheel into a new venv, offline:

```text
python -m venv .venv
# Use .venv/Scripts/python on Windows, .venv/bin/python on macOS/Linux:
python -m pip install --no-index /path/to/journal_quality_assessment_skill-0.2.0-py3-none-any.whl
jqa doctor
jqa demo --output /path/to/user-data/demo-001
```

Use the virtual environment's interpreter for pip, and activate it or invoke its
bin/Scripts executable explicitly. Source installation also works with
`python -m pip install --no-build-isolation .`; the bundled backend needs no downloads.
The demo uses explicitly fictional bilingual papers, fabricated DOI-shaped test IDs,
and fixed Mock fixtures. It is not a research result and cannot rate user papers.

## Your data

UTF-8 CSV (optional BOM), columns `doi,journal_name,title,abstract,year`. One article
per record; quote multiline/comma-containing fields. DOI syntax is currently required.
Validate missing fields, duplicates/conflicts, article types and identity clues.
Keep data/output outside the installed code. No private reference database is needed.

```text
jqa validate --csv /path/to/papers.csv --output /path/to/user-data/check.json
jqa prepare --csv /path/to/papers.csv --mode multi --provider deepseek --output /path/to/user-data/run --run-id my-study --budget-usd 2
jqa api-estimate --run /path/to/user-data/run
jqa run --run /path/to/user-data/run --max-tasks 10 --allow-paid --confirm-send
jqa resume --run /path/to/user-data/run --allow-paid --confirm-send
jqa export --run /path/to/user-data/run --output /path/to/user-data/export
```

`--mode single` requires one journal; multi requires two or more. `--provider agent`
exports local tasks instead of using an API. All runs have distinct evaluator IDs.
API mode freezes model/thinking, concurrency, retry, token and cumulative budget
parameters from config/deepseek.example.json. No model mixing or score imputation.

## Credentials, consent and cost

No environment variable is mandatory. Use hidden terminal input or an ignored
`.local/credentials.json` with an api_key field and `--credentials`. Never send keys
in chat or commit them. Hidden-input fallback with echo is refused. Paid commands
display estimates and transmitted fields before network access. `--allow-paid` plus
`--confirm-send` represents explicit reviewed consent; an interactive SEND prompt is
otherwise required. No credentials are needed for validation, preparation or demo.
Budget values are USD; a CNY budget requires an explicit user-supplied exchange rate
and date. Estimates/reservations are not the provider's final invoice or a guaranteed
invoice ceiling. Prices must be reviewed before real use.

## Method and outputs

Complete graphs for small datasets, the preserved K-regular design for larger ones.
Each article gets independent integer1–4 Originality, Significance and Rigour, evidence
and uncertainty. Compute each article's own valid means, then equal-article journal
means. No reputation, uncertainty weighting or zero-filling. Pinned Prompt1.2/Rubric1.1
and hashes are retained. Export articles.csv, journals.csv, scores.json, coverage,
dimension distributions, population SD, evidence, raw responses and execution records.
Incomplete runs need `--allow-partial` and are labelled partial_not_final. Rankings
require separate calibration evidence; one observation's SD=0 does not show stability.

## Maintenance and troubleshooting

Managed repeat install is idempotent. `scripts/install.py ... --upgrade` backs up
old code; `--uninstall` removes only the managed Skill, not user runs. For wheel
installs use pip upgrade/uninstall in the selected venv. Preserve the exact code
version needed by frozen runs. After moving a run, `relocate --confirm-run-id` backs
up/rebinds physical paths without changing assessment rows. Unknown active attempts
must first be reviewed. Filesystem hard-link support is diagnosed, never replaced
with unsafe overwrites. Missing Python, denied HTTPS, balance/rate errors, invalid
JSON and unknown POST delivery have explicit stops; see the workflow reference.

Read PRIVACY.md, SECURITY.md and NOTICE. Only anonymized fields are selected for
third-party transmission, but article text can still disclose identity; users need
the right to process/send it. This is not a multi-tenant service. MIT covers software
and synthetic examples, not article rights or provider services. CITATION.cff records
software version and the method paper; no invented repository URL/DOI is supplied.
GitHub publication and plugin-directory approval are pending, not performed here.


## Authors / 作者
Jiangbo Li¹˒², Mingyue Zhang¹, Shihang Niu¹

李江波¹˒²，张明月¹，牛士航¹

1. Business College, Qingdao University, Qingdao 266100, China.
   青岛大学商学院，山东 青岛 266100。
2. Research Institute for Science and Technology System and Institutional Innovation of Qingdao, Qingdao 266100, China.
   青岛市科技体系与制度创新研究基地，山东 青岛 266100。

- Jiangbo Li / 李江波：Research guidance and supervision / 研究指导。
- Mingyue Zhang / 张明月：Independently completed the main code development and implementation of the JQA Skill / 独立完成 JQA Skill 的主要代码开发及软件实现。
- Shihang Niu / 牛士航：Provided theoretical foundations and methodological references based on previously published research / 提供 JQA 评价框架的理论基础及方法参考。

## Theory and implementation / 理论来源与实现
Li, Jiangbo; Niu, Shihang; Jiang, Chunlin (2026). From paper content to journal quality: an LLM-based comparative assessment approach within the REF 2021 framework. Scientometrics, 131(11), 8617–8643. https://doi.org/10.1007/s11192-026-05838-0

Publisher verification: https://link.springer.com/article/10.1007/s11192-026-05838-0 (2026-11-17); published online 28 September 2026; issue 11 cross-checked with RePEc: https://ideas.repec.org/a/spr/scient/v131y2026i11d10.1007_s11192-026-05838-0.html .
原论文提供理论、评价指标及计算方法来源，包括 LLM_REF 和 Bradley–Terry BT_score；这些既有方法不是 JQA 首创。JQA 是相关方法的程序化实现与工程适配，不是原论文的完整复现，未实现 BT_score。张明月独立完成的软件工程实现及新增工程功能包括 CLI、输入校验、配对流程编排、恢复、审计和导出；不将这些工程功能归作原论文的新理论。论文作者与软件作者分别署名，不互相替代。
The paper supplies the theoretical and methodological reference; JQA implements engineering adaptations. Existing methods are not claimed as JQA inventions. No paper figures, code or extended copyrighted text are reproduced.

## License and rights / 许可证与权属
JQA uses the MIT License. Copyright (c) 2026 Jiangbo Li, Mingyue Zhang, Shihang Niu
作者已确认三位作者共同持有版权、同意 MIT 开源，不存在已知第三方代码或材料授权限制；学校知识产权归属不涉及本项目。

## Release status / 发行状态
Publisher / 发布主体：Mingyue Zhang（张明月）个人 GitHub 账号；仓库名 journal-quality-assessment。Windows offline tests verified; Ubuntu/macOS and hosted CI NOT RUN. Scope: Codex Skill + Python core, DeepSeek API; MCP excluded.
