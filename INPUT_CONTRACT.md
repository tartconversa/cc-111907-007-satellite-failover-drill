# 卫星备援演练的覆盖盲区与补演安排：资料契约

所有资料为本地合成记录，不包含真实个人或企业数据。业务含义以下文及字段字典为准。

terminal 和 operator 在工作期间位于任务站点，SAT 为共享通道资源，无须实际转移但占用容量。业务同时占用兼容终端、合格操作员和一个通道槽。depends 表示前项完成后才可开始。deadline 是开始而非完成期限。单站成功不证明同情景资源可复用；同设备同配置证据仅支持对应业务能力。借用不同设备或改变配置要单独标注，不补造原计划证据。

时间均带时区并以 UTC 记录，区间左闭右开；received 是事实进入系统的时点，不可在更早的分析中使用。null 代表缺失或尚未知，不能默认0或正常。输入顺序不代表业务顺序。来源主键标识事实，带版本的来源行以来源身份和版本解释，来源版本冲突须保留问题。重复投递同一事实不产生新的业务活动。

## sites

站点位置决定终端转移耗时。

| 字段 | 含义 |
|---|---|
| `site` | 站点 |
| `region` | 区域 |

## resources

全局共享资源，不按站点复制。

| 字段 | 含义 |
|---|---|
| `resource` | 资源 |
| `type` | terminal/channel/operator |
| `capacity` | 同时承载数量 |
| `initial_site` | 初始站点 |
| `skills` | 资格/能力 |

## transfers

有向移动分钟数，未列路径不可假设瞬移。

| 字段 | 含义 |
|---|---|
| `from_site` | 起点 |
| `to_site` | 终点 |
| `minutes` | 分钟 |

## business

同情景业务依赖与恢复承诺。

| 字段 | 含义 |
|---|---|
| `business` | 业务 |
| `site` | 站点 |
| `kind` | voice/data |
| `deadline_minutes` | 中断后最晚开始分钟 |
| `duration_minutes` | 持续分钟 |
| `depends` | 需先完成业务 |
| `priority` | 优先级 |
| `config` | 配置版本 |

## scenarios

情景内 outage_sites 同时发生中断。

| 字段 | 含义 |
|---|---|
| `scenario` | 情景 |
| `start` | 起点 |
| `outage_sites` | 受影响站点 |
| `duration_minutes` | 中断持续 |

## evidence

演练步骤证据，由 observation 标识独立观察。

| 字段 | 含义 |
|---|---|
| `observation` | 观察记录 |
| `drill` | 演练身份 |
| `business` | 业务 |
| `config` | 配置 |
| `resource` | 终端 |
| `source` | machine/observer |
| `result` | success/fail |
| `start` | 实测开始 |
| `end` | 实测完成 |

## 后续资料

这些记录是可扩充的初始事实，不是固定答案。使用者需能导入相同契约的新记录，并通过应用登记题述的人工作业。新增的业务状态、使用入口及存储由实现者设计；来源资料文件只读，结果放入单独的工作目录，互不相关的工作目录不能相互污染。
