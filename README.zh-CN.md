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

下表按上游集成 README 和 API 映射说明数据时效，而不是接口名称。

| 数据 | 来源 | 时效 |
| --- | --- | --- |
| 余额和欠费 | `queryUserAccountNumberSurplus` | 实时 |
| 当前阶梯、剩余电量和电价 | `queryDayElectricChargeByMPoint` 的阶梯字段 | 实时 |
| 昨日用电量 | `queryDayElectricByMPointYesterday` | 实时 |
| 每日用电量、每日费用、月度汇总 | `queryDayElectricByMPoint` 和 `queryDayElectricChargeByMPoint` | 当月通常延迟约两天 |
| 当年和上年汇总 | `getAnalyzeFeeDetails` | 非实时；当年数据更新至上月 |

“实时”的昨日用电量仍是按天的数据，不是实时功率或当天累计用电量。

## 实体

每个缴费户号会提供以下传感器：

- 能源累计用电量：供 Home Assistant 能源面板使用的累计 `kWh`。
- 已结算累计费用：供能源面板成本跟踪使用的累计 `CNY`。
- 昨日用电量、余额和欠费。
- 当前阶梯、剩余电量和电价。
- 最近结算日用电量和费用。
- 本月、上月、今年和去年用电量及费用。

只有“能源累计用电量”和“已结算累计费用”使用 `total_increasing` 状态类。其余查询快照使用 `measurement` 或不设置状态类，避免 Home Assistant 将月度或年度快照误认为持续增长的电表读数。

## 能源面板和账单修正

能源累计用电量以昨日完整用电量为依据，并在当前日期内按时间比例平滑推进；账本本身仍只保存完整日读数，因此不会因插值重复计量。延迟的每日账单可用后，集成会将账单中的 `result[].power` 与 `result[].charge` 同已存储数据比较，并修正对应日期的 Home Assistant Recorder 历史统计。

累计实体本身不会因历史账单修正而下降；修正通过 Recorder 的历史统计完成。首次安装不会根据旧账单虚构历史实体状态。费用仅取已结算的每日 `charge`，不会用当前阶梯电价估算。

在 Home Assistant 能源面板中，请选择：

- 用“能源累计用电量”作为电力消耗来源。
- 用“已结算累计费用”作为总费用实体来源。

## 安装

通过 [HACS](https://hacs.xyz/) 安装，或从 [orangeboyChen/ha-csg](https://github.com/orangeboyChen/ha-csg/releases) 下载发行版本。

当前开发和测试基线为 Home Assistant Core `2026.9.3` / Python `3.14.2`。其他 Home Assistant 版本可能可用，但未经本项目测试，不属于保证的兼容范围。

## 升级到 v2 的破坏性变更

版本 2 将集成域从 `china_southern_power_grid_stat` 改为 `csg`，不提供自动迁移。

1. 删除旧集成。
2. 重启 Home Assistant。
3. 重新添加 **CSG** 并配置账户。
4. 按需删除旧设备、实体和历史统计。

## 更新间隔

余额、欠费、阶梯状态和昨日用电量按配置的间隔刷新，默认四小时。每日账单详情、月度汇总和年度汇总每天刷新一次。

## API 实现

[`custom_components/csg/csg_client/__init__.py`](custom_components/csg/csg_client/__init__.py) 实现了南网 App API，也可独立使用。基本示例见 `csg_client_demo.py`。

## 致谢

- [CubicPill/china_southern_power_grid_stat](https://github.com/CubicPill/china_southern_power_grid_stat)，上游项目。
- [lyylyylyylyy](https://github.com/lyylyylyylyy)，上游短信验证码登录支持。
