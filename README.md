# 随访节律差异下的服务覆盖与失访偏倚

项目使用 Python 3.11 标准库与 SQLite，现有逐案处理入口为 `survivorship_followup.app.followup_cli`。保留原有处理器、持久化与测试；分析业务使用本地合成资料，不连接生产系统。无需 pip 安装、下载模型或启动额外服务。

## 现有程序

在仓库根目录运行 `bash scripts/demo.sh`，依次完成现有产品回归、分析输入核对、语法构建和真实业务入口调用。所有运行文件位于临时目录，结束后清理；运行失败立即退出，不使用预存输出代替执行。

单独测试：`python3 -m unittest discover -s tests/journey_followup -p 'test_*.py' -v`。

输入检查：`python3 -m unittest discover -s tests -p test_analysis_inputs.py -v`。

构建检查：`python3 -m compileall -q survivorship_followup tests`。

业务调用：`python3 -m survivorship_followup.app.followup_cli fixtures/demo.json --db /tmp/survivorship_followup-demo.sqlite3`。

## 分析资料与联调

`fixtures/analysis/input.json` 保存业务演练数据，字段解释在 `fixtures/analysis/fields.md`。以下已核对的数据形态用于分析回归：revised_plan、revised_response、team_transfer。现有输入测试检查这些形态是否真实存在，不代替分析方法或数值正确性的验证。

分析入口接通后执行 `bash scripts/feature_demo.sh`：先重跑现有产品，再从干净数据库计算分析、同输入重放、核对输出结构并打印完整 JSON。尚未接通时该命令会报模块不存在，不会用静态样例冒充成功。分析 ID 为 `study_id`，其余输出包括 `opportunities, coverage, standardized_comparisons, dropout_sensitivity, revisions`。方法正确性还需覆盖业务说明中的反例，结构核验不能代替统计验证。

### 覆盖研究模块 `survivorship_followup.coverage_study`

入口（与 feature_demo 一致）：

```
python3 -m survivorship_followup.coverage_study \
  --input fixtures/analysis/input.json \
  --database <followup_db.sqlite3> \
  --output <coverage.json>
```

模块只评价服务触达，不评价治疗效果，口径要点：

- **机会重建**：计划区间左闭右开，`supersedes` 自新计划 `start` 起截断旧计划；退出时刻之后不再排机会；晚于 `as_of` 的机会预排为 `open`，窗口未关闭且无应答为 `unknown`，窗口关闭无应答才记 `missed`。
- **应答合并**：按 `opportunity_at` 归并，`responses.supersedes` 链只保留最终应答，同一机会补交/重发至多计一次成功；回补延迟 = `received_at - opportunity_at`；无对应计划机会或晚于窗口的应答进 `unmatched_responses` 并注明原因，不充数。
- **覆盖分组**：按团队、入组届次、服务需求层及三维交叉单元给覆盖率（分母只含到期机会）、最长连续中断和回补延迟分布；无样本支撑的单元保留 `null`，不硬外推。
- **标准化比较**：`reference_plan` 把所有人放到统一参考节律（study.reference_every_days）下重排机会，剥离计划密度差异；`reference_population` 按需求层做直接标准化，缺支撑层返回 `null`；同时并列原始团队率供对照。
- **失访敏感性**：未回复者问卷真值不可推断，对到期未覆盖机会施加完成概率区间（study.dropout_completion_range，默认 0–1），输出最差/最佳情形界值、五点情景网格、个人与团队情景率及团队排序翻转点（tipping points），不用已回复者均值冒充全体。
- **可重放与衔接**：`study_id` 由输入内容指纹决定，同输入重放字节一致；研究运行幂等写入同一 SQLite 台账的 `coverage_study_runs` 表，并经 `baseline_case_id` 关联 enrollment/timewheel/merge 既有运行。

方法回归测试：`python3 -m unittest discover -s tests/journey_followup -p test_coverage_study.py -v`。

## 离线环境

使用 Python 3.11.2 的 Linux 环境即可复现。容器环境预置解释器后，以只读方式挂载仓库，将文件复制到容器临时目录再运行脚本；运行时关闭网络。基础测试和小规模本地数据适合在 90 秒内展示，正式展示命令为 `bash scripts/feature_demo.sh`。不要在展示脚本里安装依赖或等待外部服务。
