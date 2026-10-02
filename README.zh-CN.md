# ha-csg

[简体中文](README.zh-CN.md) | [English](README.md)

适用于中国南方电网用电数据的 Home Assistant 自定义集成。

本项目 Fork 自 [CubicPill/china_southern_power_grid_stat](https://github.com/CubicPill/china_southern_power_grid_stat)。
它使用 `csg` 集成域，并重新设计实体，使其符合 Home Assistant 长期统计和能源面板的语义。

## 功能

- 支持广东、广西、云南、贵州、海南的南方电网账户。
- 支持短信、密码加短信、南网 App 扫码、微信扫码和支付宝扫码登录。
- 支持多个南网账户，以及每个账户下多个缴费户号。
- 使用 Home Assistant 图形化配置流程，不支持 YAML 配置。
- 在设备和实体名称中保留完整缴费户号。

## 数据时效

当前 production 使用已发布的真实日电量和正式账单快照。

| 数据 | 来源 | 时效 |
| --- | --- | --- |
| 余额和欠费 | `queryUserAccountNumberSurplus` | 最新账户快照 |
| 昨日和近期日电量 | `get_month_daily_usage_detail()` / `queryDayElectricByMPoint` | 已发布的日事实；昨日未发布则 unavailable |
| 广州阶梯快照 | 当月日用电量 | 保留现有普通单户计算，不实现 tariff profile |
| 上一结算月费用、年用电量及费用 | `get_year_month_stats()` / `getAnalyzeFeeDetails` | 正式账单快照；今年费用与用电量结算至上月 |

日电量不是实时功率或今日累计用电量。当前 daily API 只返回 kWh，因此在没有权威
charge 输入时，最近结算日费用和本月费用保持 unavailable。上月、今年和去年费用
继续使用已批准的正式数据源，不反推每日费用。

## 实体

每个缴费户号会提供以下传感器：

- 昨日用电量、余额和欠费。
- 当前阶梯、剩余电量和电价。
- 最近结算日用电量和费用。
- 本月、上月、今年和去年用电量及费用。

查询快照使用 `measurement` 或不设置状态类。“能源累计用电量”和“已结算累计费用”
已退役，不再创建；不会用另一种 `total_increasing` sensor 替代它们。

## 正式能源统计与 M5 升级迁移

正式 Energy consumption 路径为：

```text
CSG daily facts → HistoryStore → EnergyStatisticsBridge
→ Home Assistant Recorder external statistics → Energy Dashboard
```

**选项 → 参数设置 → 启用外部能源统计** 默认**开启**。旧 entry 缺少此字段时，升级后
视为启用；新 entry 默认启用。用户明确设置的 **False** 会继续保留，该关闭状态下
集成不执行任何 Recorder 读取或写入。每个缴费账号的稳定标识是
`csg:energy_<full SHA-256>`，显示名称为不含敏感信息的 `CSG energy <前 8 位 hex>`。
完整 SHA-256 由缴费户号生成。

集成不自动修改能源面板 preferences。请对每个账户手动操作：

1. 确认新 external statistic 已生成。
2. 将对应账户的消费来源切换到 `csg:energy_<full SHA-256>`。
3. 同一账户不要同时配置旧 Energy total 和新 external statistic，否则会重复统计。

旧 Energy total / Settled cost total 不再由集成创建。Home Assistant 可能保留
unavailable/restored registry state，这是允许的。旧 entity registry 项和 Recorder
history/statistics 不会自动删除。旧 `csg.energy_ledger.<entry_id>` Store 留在磁盘上，
成为 inert legacy data：不读取、不写入、不迁移、不删除。是否清理这些旧数据属于
后续独立的用户操作。

M5 暂无 external cost statistic，也不再推荐 Settled cost total 作为费用来源。
Tariff profile、多人额度、TOU、历史日电费推导及月账单按日分摊留待 M6；本阶段
不使用 current tariff × kWh 反算费用。

安全回退目标是已审计的 M4 merge：
`e4af90b31e7a2ee9902adbb8255ed3559e9d93a3`，继续使用 HistoryStore → Bridge。
不推荐恢复已退役的 ledger、interpolation 或 Recorder correction 路径。

每个已发布日的真实电量（包括真实零值）在 `Asia/Shanghai` 当日零点生成一个统计点；
缺日保持缺失，不制造小时分布，因此小时图可能稀疏。月账单与对账结果不会改写日电量。
历史修订和后来发布的缺日会在后续同步或重启时重新收敛。
已排队的 import 会先读回确认，再与最新持久事实做最终比较。未确认 import 的所有权在
集成 reload 期间保留于内存；Recorder 暂时故障只会推迟收敛，不会丢弃该所有权。
普通卸载先停止数据生产者，再确认 import；HA 全局停机取消等待，进程重启后重新比较
已提交的数据库。关闭选项后停止访问 Recorder，
已有统计保留。本阶段没有删除功能、外部费用统计或电价计算。

## 安装

通过 [HACS](https://hacs.xyz/) 安装，或从 [orangeboyChen/ha-csg](https://github.com/orangeboyChen/ha-csg/releases) 下载发行版本。

当前开发和测试基线为 Home Assistant Core `2026.9.3` / Python `3.14.2`。其他 Home Assistant 版本可能可用，但未经本项目测试，不属于保证的兼容范围。

## 升级到 v2 的破坏性变更

版本 2 将集成域从 `china_southern_power_grid_stat` 改为 `csg`，不提供自动迁移。

1. 删除旧集成。
2. 重启 Home Assistant。
3. 重新添加 **CSG** 并配置账户。
4. 按上述 M5 迁移说明配置 external statistic。

集成域变更与 M5 退役属于不同迁移；历史数据不会自动清理。

## 更新间隔

余额、欠费、阶梯状态和昨日用电量按配置的间隔刷新，默认四小时。每日账单详情、月度汇总和年度汇总每天刷新一次。

## API 实现

[`custom_components/csg/csg_client/__init__.py`](custom_components/csg/csg_client/__init__.py) 实现了南网 App API，也可独立使用。基本示例见 `csg_client_demo.py`。

## 致谢

- [CubicPill/china_southern_power_grid_stat](https://github.com/CubicPill/china_southern_power_grid_stat)，上游项目。
- [lyylyylyylyy](https://github.com/lyylyylyylyy)，上游短信验证码登录支持。
