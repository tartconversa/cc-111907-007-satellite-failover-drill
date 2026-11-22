# 卫星备援演练的覆盖盲区与补演安排

多站点同时断网时，单站演练成功不能回答"哪些业务真能保住"：移动终端在站点间共享、卫星通道槽位有限、操作员资格不可重复计算。本应用把**站点、共享设备、业务依赖、实测证据与应急情景**放在同一套离线模型中，先形成有证据支撑的应急安排，再据此组织下一次演练。

应用只做**离线业务分析与计划登记**：不连接任何网络、不控制卫星终端、不执行攻击或入侵演练，仅依赖 Python 3.14 标准库。

## 一、业务规则如何落地

| 题述要求 | 应用中的保证 |
|---|---|
| 设备转移需要时间，未列路径不可瞬移 | `scheduler.shortest_travel` 只沿 `transfers` 有向图求最短路；独立校验逐段核对转移路径与耗时 |
| 通道容量、终端、操作员不能重复计算 | 每次占用逐分钟统计并发，超过 `capacity` 的计划直接判违规；SAT 按容量 2 计并发，终端/操作员容量 1 严格排他 |
| 业务有最晚开始时间与先后依赖 | `deadline_minutes` 是**开始**期限；拓扑排序保证依赖完成后才开始；违规计划不能登记 |
| 计划版本、设备配置、业务步骤相符才支持能力判断 | 证据按（业务 × 配置 × 终端）三元组归总；配置不符判"配置不适用" |
| 机器日志与观察员冲突显出争议 | 同演练同时间窗两来源结果不一致 => `disputed`，不得当作已证实能力 |
| 补交同次演练不产生新独立证据 | 独立证据以不同 `drill` 计；同一 `observation` 重复投递直接拒绝 |
| 资料不够只能列为待确认假设 | 无相符证据 => `unconfirmed`，排程可满足也明确标注"仅为待确认假设"，不被其他成功步骤掩盖 |
| 不提交重复使用资源或违反依赖的计划 | 每个方案都经过 `validate_plan` 独立复核，登记接口只接受 `valid` 计划 |
| 关键业务达不到要说明放弃次要业务是否仍不够 | `critical_gaps` 枚举只保留关键业务的方案族，给出"放弃 X 仍不足"的明确结论 |
| 筹备占用设备与演练时间，取消才释放 | 安排/演练登记即锁定资源，重叠时间窗重复确认同一终端被拒绝；`cancel` 后释放 |
| 回填后按原情景核对，区分三类失败 | 核验结论分为流程执行失败 / 资源不够 / 配置不适用 / 证据争议 / 缺少证据 |
| 共享设备能力不足 => 引用它的未执行安排重评估 | `reevaluate` 只动未执行安排；已执行演练保留当时快照与差异，不用新证据重写 |
| 临时借调设备不证明原配置 | 借调观察单独标注，借调成功不把原配置能力升级为已确认 |
| 补演针对未支撑承诺与真实争议，并遵守既有占用 | `supplement` 列出补演目标与必须避让的占用清单 |
| 从承诺追溯全貌、重启后继续 | `trace` 汇总情景/占用/证据/现场结果/待办；状态原子写入独立工作目录 |

## 二、目录结构

```
emergency_prep/          应用包（仅标准库）
  inputs.py              只读源资料加载与契约校验
  timeutil.py            UTC 时间解析（左闭右开）
  evidence.py            证据台账：去重、争议、能力三元组判断
  scheduler.py           转移图、方案枚举、贪心模拟、独立计划校验、瓶颈分析
  analysis.py            情景报告：承诺核对、关键缺口、方案对比
  preparation.py         筹备占用、演练登记、回填核验、重评估、补演目标
  store.py               工作目录状态持久化（原子写入）
  trace.py               承诺反向追溯与待办动作
  cli.py                 中文命令行入口
scripts/run_isolated.py  处理外部不可信文件时的隔离模式运行器
tests/                   56 个业务测试 + 3 个原始资料契约测试
inputs/                  只读合成源资料（不修改、不写入）
```

## 三、环境与检查

- Python 3.14，无第三方依赖。
- 运行全部测试：

```bash
python -m unittest discover -s tests -v
```

- 容器内独立复核（与原有方式一致）：

```bash
docker build -t local-analytics-inputs .
docker run --rm --network none local-analytics-inputs
```

## 四、快速上手（完整一轮工作流）

以下命令在仓库根目录执行；`--workdir` 指定独立工作目录，省略时默认为 `./work`，也可用环境变量 `EMERGENCY_PREP_WORKDIR`。全局参数（`--workdir/--json`）放在子命令前后均可。

### 1. 初始化工作目录并导入只读证据

```bash
python -m emergency_prep.cli init --inputs inputs --workdir work
python -m emergency_prep.cli import-evidence --workdir work
python -m emergency_prep.cli evidence --workdir work
```

`evidence` 会直接显示争议，例如 `dispatch-A/v2/T1` 的机器日志成功、观察员失败，被标为"有争议"；`call-B` 只有 v1 证据，v2 能力保持"待确认假设"。

### 2. 选择多站点中断情景做覆盖分析

```bash
python -m emergency_prep.cli scenarios --workdir work
python -m emergency_prep.cli analyze dual --workdir work
python -m emergency_prep.cli compare dual --workdir work
```

`analyze` 输出：推荐方案的分钟级时间表与转移记录、每条承诺的可满足性与证据状态、最先失守的依赖、共享瓶颈，以及不同**设备预置 × 业务次序 × 操作员裁决**方案的对比。加 `--json` 输出机器可读结果。

关键业务在全部方案族中都无法满足时，报告明确写出例如"即使放弃全部次要业务（report-C），call-B 仍无法在截止时间内满足"，不会生成表面全覆盖的清单。

### 3. 确认筹备安排（占用设备与时间窗）

```bash
python -m emergency_prep.cli book dual --workdir work \
  --title "双站中断首轮演练安排" \
  --window-start 2026-11-25T08:00:00Z \
  --window-end 2026-11-25T10:00:00Z
```

重叠时间窗再登记同一终端会被拒绝（退出码 2）并列出冲突资源。`--plan-index N` 可选用 `compare` 中的其他可行方案；只有通过独立校验的方案才能登记。

### 4. 登记演练、回填实际步骤并核验

```bash
python -m emergency_prep.cli register-drill dual --workdir work \
  --title "首轮演练" \
  --window-start 2026-11-25T08:00:00Z \
  --window-end 2026-11-25T10:00:00Z
```

回填文件是步骤数组（每行含 business/config/resource/source/result/start/end，可选 `borrowed` 与 `observation`）：

```bash
python -m emergency_prep.cli backfill DR1 /path/to/actual_steps.json --workdir work
```

回填后按**原情景与原计划快照**逐条核验，结论分为：证据支持 / 流程执行失败 / 资源不够 / 配置不适用 / 证据争议 / 缺少证据；同时自动重评引用同一共享设备的未执行安排。借调设备请在该行标 `"borrowed": true`，其成功不会证明原配置。

### 5. 安排补演、管理待办、追溯承诺

```bash
python -m emergency_prep.cli supplement dual --workdir work
python -m emergency_prep.cli action-add --workdir work \
  --title "安排 T2 的 v2 配置补演" --businesses call-B --reason "借调 T1 不证明 T2 能力"
python -m emergency_prep.cli trace call-B --workdir work
python -m emergency_prep.cli status --workdir work
```

### 6. 取消与释放

```bash
python -m emergency_prep.cli cancel AR1 --reason "设备改期" --workdir work
```

已执行的演练及其安排不可取消（只能保留当时记录与差异）；未执行安排取消后立即释放资源，关联的未执行演练一并释放。

## 五、命令一览

| 命令 | 作用 |
|---|---|
| `init` | 初始化独立工作目录 |
| `scenarios` | 列出多站点中断情景及涉及业务 |
| `import-evidence [--file]` | 导入源证据或同契约新证据，自动去重 |
| `evidence` | 证据台账、能力状态与争议明细 |
| `analyze <情景>` | 业务覆盖分析（承诺/缺口/瓶颈/对比） |
| `compare <情景>` | 全部确定性方案的可行性对比 |
| `book <情景>` | 确认筹备安排并锁定资源与时间窗 |
| `register-drill <情景>` | 登记演练并锁定资源 |
| `cancel <安排ID>` | 取消未执行安排、释放资源 |
| `backfill <演练ID> <步骤文件>` | 回填实测并按原情景核验、触发重评估 |
| `reevaluate` | 按最新证据手动重评未执行安排 |
| `supplement <情景>` | 补演目标与必须遵守的既有占用 |
| `trace <业务>` | 一条承诺的完整追溯 |
| `action-add / action-close` | 待落实动作登记与关闭 |
| `status` | 工作目录总览（重启后继续筹备的入口） |

## 六、人工操作约定与安全边界

1. **源资料只读**：`inputs/` 不被应用写入；新证据通过 `import-evidence --file` 导入，文件需符合 `INPUT_CONTRACT.md` 字段契约。
2. **工作目录隔离**：不同筹备线使用不同 `--workdir`，状态互不污染；状态写入采用临时文件 + 原子替换，中断不会损坏。
3. **不可信外部文件**：外部文件放在独立空目录，路径作为参数传入；推荐用隔离运行器，避免当前目录被植入的同名模块被加载：

   ```bash
   python3 scripts/run_isolated.py import-evidence \
       --workdir work --file /data/incoming/extra_evidence.json
   ```

4. **时间规范**：所有时间为带时区 UTC（`...Z`），区间左闭右开；`received` 是事实进入系统的时点，`as_of` 快照保证事实不能在更早的分析中被使用。
5. **能力边界**：应用不寻找数学最优，只在确定性方案族中枚举并保证硬约束；不控制任何设备、不发起网络连接、不执行攻击或入侵动作。
6. **数据合成**：输入为本地合成记录，不含真实个人或企业数据；正式演练使用本地输入与本地状态，不需要真实外部账号。
