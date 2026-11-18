# Journal Quality Assessment · 公开发行版 0.2.0

[English](README_EN.md) · [使用与恢复](references/public_workflow.md) · [方法](references/public_method.md) · [合成输出示例](references/output_example.md)

从用户提供的标题和摘要，对论文进行REF-inspired三维评价，计算论文LLM_REF和期刊等权均值。
支持中文/英文CSV、Agent文件交互与DeepSeek独立API运行、均衡配对、检查点、原始响应留存、
费用预留、恢复和CSV/JSON报告。不是官方REF评分，也不是原论文全部实验的完全复现。
不计算BT_score。无API Key仍可检查数据、准备任务和执行合成演示。

## 安装与首用

需要Python3.10+（含sqlite3与venv），运行时无第三方Python依赖。
解压到任意目录；代码与用户研究输出必须分开。路径可含空格、中文。

```text
python scripts/public_cli.py doctor
python scripts/install.py --project /path/to/your/project
```

也可使用 `--user` 安装至当前用户的 `.agents/skills/journal-quality-assessment`，
或 `--target /path/to/journal-quality-assessment` 指定独立目录。不会覆盖已有非托管Skill。
重启/刷新受支持Agent后用 `$journal-quality-assessment`；本包没有替你修改Agent全局配置。
没有Agent也能在干净虚拟环境安装Release wheel：

```text
python -m venv .venv
python -m pip install --no-index /path/to/journal_quality_assessment_skill-0.2.0-py3-none-any.whl
jqa doctor
```

上面的pip命令应使用虚拟环境内的Python：Windows为`.venv/Scripts/python`，
macOS/Linux为`.venv/bin/python`。`jqa`位于相应Scripts/bin，激活环境后可直接调用。
也可从源码执行 `python -m pip install --no-build-isolation .`，自带构建后端不下载依赖。

## 合成演示和输入

```text
python scripts/public_cli.py demo --output /path/to/user-data/demo-001
python scripts/public_cli.py validate --csv /path/to/papers.csv --output /path/to/user-data/validation.json
```

assets中四篇演示论文/DOI/摘要全部虚构，固定Mock分数只检查软件流程，不能当作科研结果。
CSV为UTF-8（允许BOM），必需列 `doi,journal_name,title,abstract,year`，每行一篇。
DOI用于本地唯一身份；当前引擎要求DOI格式。摘要含逗号/换行需正确CSV引号。
标题/摘要不自动翻译、截断或清除身份词。检查重复DOI、冲突和非研究记录后再正式评价。

## 自己的数据与DeepSeek

```text
python scripts/public_cli.py prepare --csv /path/to/papers.csv --mode multi --provider deepseek --output /path/to/user-data/my-run --run-id my-study --budget-usd 2
python scripts/public_cli.py api-estimate --run /path/to/user-data/my-run
python scripts/public_cli.py run --run /path/to/user-data/my-run --max-tasks 10 --allow-paid --confirm-send
python scripts/public_cli.py resume --run /path/to/user-data/my-run --allow-paid --confirm-send
python scripts/public_cli.py export --run /path/to/user-data/my-run --output /path/to/user-data/export-001
```

正式命令先展示估算和向DeepSeek发送的字段；`--allow-paid --confirm-send`表示审查后的明确授权。
不加confirm-send的交互终端需要输入SEND；非交互环境不会隐含授权。
Key默认在本地隐藏输入，不能安全关闭回显则拒绝；可用忽略的`.local/credentials.json`
（只含api_key字段）与 `--credentials`，不要求系统环境变量。不要将Key发送给Agent聊天。
API配置示例为config/deepseek.example.json；模型、并发、USD费用预留和任务总上限在准备时冻结。
“最多10元”需提供 `--budget-cny 10 --usd-to-cny RATE --fx-date DATE`，RATE必须由用户确认，
程序不会编造汇率。费用基于usage和配置价格估算，不是服务商最终账单硬上限。
使用Agent文件模式时改为`--provider agent`，流程见使用手册。

## 方法与输出

小样本全配对，大样本采用现有K-正则设计，所有论文计划参与次数一致，不限制同刊/跨刊边。
每篇每次的Originality/Significance/Rigour均为1–4整数，附文本依据和不确定性。
先平均各篇有效评价，再对期刊不同论文等权平均；缺失不填0，不混合模型/运行。
导出articles.csv、journals.csv、scores.json、进度/覆盖率、维度分布、论文内标准差、
原始响应和执行审计。部分运行必须显式`--allow-partial`且标记非最终。
输出journal均值不自动证明跨学科、跨模型或跨语种排名可比。

## 升级、卸载与迁移

新版本运行 `python scripts/install.py --project /path/to/project --upgrade`。
相同版本和哈希重复安装为幂等；升级备份原代码，绝不删除用户数据。
已冻结运行应使用相同代码版本；不要绕过代码哈希检查以强行用新引擎恢复。
卸载托管Skill使用同样目标并加`--uninstall`；pip版本在对应环境运行
`python -m pip uninstall journal-quality-assessment-skill`。两者均不删用户数据。
移动用户运行后使用`relocate --run PATH --confirm-run-id ID`，它备份并审计物理路径重绑定，
不改论文评分/执行事件。未知进行中尝试必须先审查。清理自己的数据见使用手册。

## 隐私、错误、引用和License

阅读[PRIVACY.md](PRIVACY.md)、[常见错误与恢复](references/public_workflow.md)、
[发布检查单](references/publishing.md)。本地元数据映射不发给模型，但文本本身可能暴露身份。
网络/余额/权限错误、无效响应、未知POST结果均不能用Mock分数填补。
软件采用MIT，用户数据和第三方论文不由该License授权；发布者版权归属确认见NOTICE。
引用CITATION.cff中的软件版本与来源论文；当前没有虚构的仓库URL、软件DOI或维护者邮箱。
这是待发布源码与Plugin打包候选，未向GitHub或任何插件目录提交。


## Methodological Foundation and References


## Release authorization pending



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
