# 机队观测接收与质量处理

设备别名和校准按有效期解析，异常观测进入隔离区。工单、分区水位和成品发布使用独立状态，不把收到数据等同于可交付。

运行需要 Python 3.11 或更高版本，使用标准库与 SQLite，无需第三方包。演练中的机构、人员、交易及读数全部为本地合成数据。

## 构建与测试

```sh
python3 -m compileall -q hangar_ingest
python3 -m unittest discover -s tests -v
```

## 本地运行

```sh
python3 -m hangar_ingest --db :memory: demo --fixture fixtures/demo.json
python3 -m hangar_ingest --db :memory: missingness-demo --fixture fixtures/missingness_demo.json
python3 -m hangar_ingest --help
```

演练使用临时数据库，不会连接远程服务。命令以 JSON 显示业务结果；测试覆盖核心状态、边界输入和错误路径。运行前可用 `python3 --version` 核对解释器版本。

## 缺测研究（missingness study）

温度均值只统计成功上传的读数，会把高负荷下网关丢点造成的偏差悄悄藏起来。缺测研究把"应上报什么"与"实际收到什么"分开核算，**只做诊断与假设边界分析，从不把估算写回正式观测，也不声称纠正了非随机缺测**。

研究通过 `study.*` 操作驱动，状态沿 `registered → gridded → modeled → scenario` 单向推进：

1. `study.create` 登记每台设备应上报的时间网格、实际运行区间与计划停机窗口，并在冻结时点快照当时有效的别名、校准与质量规则（含规则版本号）。
2. `study.grid` 把每个格子唯一判为四类之一：
   - `valid`：冻结别名可解析、校准在有效期内覆盖、质量规则通过的接收观测；
   - `quality_excluded`：数据到达但被隔离，或在冻结参考数据下失去别名/校准/质量有效性；
   - `planned_outage`：落在登记停机窗口内；
   - `unexpected_missing`：应报而无可用读数。

   同一格子的密集重复上报只计数、不增加覆盖；长缺口保留为一串独立缺测格，不存在跨窗插值。
3. `study.conditions.import` 以版本化输入接入工况表，按有效期连接到格子并按设备、时段与工况层分层。工况表只允许使用截止时可得的工况字段，不能包含缺测格的真实温度或事后维修结论。
4. `study.strata_report` 与 `study.grid_report` 报告分层覆盖率、缺口长度分布与最长意外缺口。
5. `study.model.fit` 用带 L2 正则的逻辑回归估计观测概率，特征只用截止时可得工况；按飞机整体留出做交叉验证，报告 Brier、分箱校准、逆概率权重的极值与有效样本量，并逐设备×工况层检查重叠支持。某层只有观测或只有缺测时，`support_ok=false`，`study.model.weighted_mean` 与情景中的 IPW 均值都不输出貌似精确的校正值。
6. `study.scenario` 接收工程师预先登记、受物理上下界约束的分层偏移假设，区分报告：
   - **可由数据识别的量**：可见格均值、可见超温比例、覆盖率；
   - **假设驱动的量**：全体运行格温度均值、超温比例的可行范围（由物理界与假设层均值推出，而非伪造完整分布）、业务判断翻转点。
   不确定性通过按飞机整体重采样（cluster bootstrap）给出。
7. `study.evidence` / `study.evidence.export` 保存网格、行级去留（含重复与隔离引用）、工况版本、模型与假设的摘要封存证据。校准日后修订只改变实时数据，研究冻结的快照与摘要保持不变，`study.replay` 可随时取回原证据。

`missingness-demo` 使用三架飞机的合成记录：低负荷时段覆盖完整，高负荷（高温）时段系统性丢点。它演示仅可见均值为何偏低、假设区间如何改变结论，以及校准修订后历史研究仍能取回原证据。

