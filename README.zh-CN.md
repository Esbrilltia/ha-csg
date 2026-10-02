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
| 当前电价和阶梯 | 显式广州当前 tariff profile；阶梯使用权威当月累计电量 | 选项确认前保持未配置；峰谷时段采用 Asia/Shanghai |
| 上一结算月和年用电量及费用 | `get_year_month_stats()` / `getAnalyzeFeeDetails` | 正式账单快照；今年费用与用电量结算至上月 |

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

**选项 → 参数设置 → 启用外部电量与费用统计** 默认**开启**。旧 entry 缺少此字段时，升级后
视为启用；新 entry 默认启用。用户明确设置的 **False** 会继续保留，该关闭状态下
集成不执行任何 Recorder 读取或写入。每个缴费账号的稳定标识是
`csg:energy_<full SHA-256>`，显示名称为不含敏感信息的 `CSG energy <前 8 位 hex>`。
完整 SHA-256 由缴费户号生成。

集成不自动修改能源面板 preferences。请对每个账户手动操作：

1. 确认新 external statistic 已生成。
2. 将对应账户的消费来源切换到 `csg:energy_<full SHA-256>`。
3. HA 全局币种为 **CNY** 时，将费用来源 `stat_cost` 设为 `csg:cost_<同一完整 SHA-256>`。
4. 同一账户不要同时配置旧 Energy total 和新 external statistic，否则会重复统计。

旧 Energy total / Settled cost total 不再由集成创建。Home Assistant 可能保留
unavailable/restored registry state，这是允许的。旧 entity registry 项和 Recorder
history/statistics 不会自动删除。旧 `csg.energy_ledger.<entry_id>` Store 留在磁盘上，
成为 inert legacy data：不读取、不写入、不迁移、不删除。是否清理这些旧数据属于
后续独立的用户操作。

M6 新增正式月账费用统计和显式当前电价配置，固定实现基线为 M5 merge：
`6c193acb50fe365c3adb4c40a040bb2b2a449ea1`。旧 ledger、插值和 correction 路径
继续保持退役，Settled cost total 不再是支持的费用来源。

每个已发布日的真实电量（包括真实零值）在 `Asia/Shanghai` 当日零点生成一个统计点；
缺日保持缺失，不制造小时分布，因此小时图可能稀疏。月账单与对账结果不会改写日电量。
历史修订和后来发布的缺日会在后续同步或重启时重新收敛。
已排队的 import 会先读回确认，再与最新持久事实做最终比较。未确认 import 的所有权在
集成 reload 期间保留于内存；Recorder 暂时故障只会推迟收敛，不会丢弃该所有权。
普通卸载先停止数据生产者，再确认 import；HA 全局停机取消等待，进程重启后重新比较
已提交的数据库。关闭选项后停止访问 Recorder，
已有统计保留，不提供破坏性删除功能。

## 正式月账费用统计

费用唯一事实源是 `getAnalyzeFeeDetails → get_year_month_stats()` 的
`actualTotalAmount → HistoryStore.monthly_bills[].cost_cny`。同一 Bridge 将它物化为
独立的 `csg:cost_<SHA-256(account_number) 完整值>`，显示名为
`CSG cost <前 8 位 hash>`，不含户号、姓名或地址。采用 HA Core 2026.9.3 第一方
Opower 的 external cost metadata：`source=csg`、`mean_type=NONE`、`has_sum=True`、
`unit_class=None`、`unit_of_measurement=None`。金额始终是 **CNY**。

HA Energy 使用单一全局币种。如果不是 **CNY**，日电量统计仍正常，集成对费用统计
执行 **0 次 Recorder 读写**，每 lifecycle 最多警告一次，并保留已有费用统计。
如需费用展示，先将 HA 币种改为 CNY，再 reload，从持久化事实建立或恢复统计。
不实现货币转换。关闭或改变 tariff profile 不会改写、删除正式费用；明确关闭
external statistics 则停止访问电量及费用统计，同时保留已有数据。

CSG 事实月份定义为 **Asia/Shanghai 自然月**。只物化早于当前上海自然月的已关闭月份；
即使上游异常返回当前月费用，也不发布为最终结算月账。整月事实的 Recorder anchor
固定为账单月 **15 日 12:00 Asia/Shanghai**，由 HA 转 UTC。
`state` 是该月正式费用，`sum` 用 Decimal 累加所有已知月账。真实零费用有效；
缺失、无效或尚未发布的月份不生成 row。缺月后来补账，或月账向上、向下修订时，
从最早变化月重新物化后续累计值，不追加 delta correction，不 adjust，不 clear。
费用沿用电量已经审计的每 statistic lane、真实回读、重试、producer barrier、
卸载收敛与进程恢复协议；电量和费用各有独立 lane。
每个含可接受月账的官方响应在完成该批 upserts 后请求 Bridge 收敛，包括相同事实的
重复获取或一次被捕获的保存异常。Bridge 通过 durable gate 重试 pending persistence；
事实与 Recorder 一致后，重复 refresh 不再产生 import。

**正式费用只有月账粒度。** HA 图表和自然月、年聚合受 HA configured timezone 影响。
月中 anchor 是整月事实的物化约定，在 IANA 时区偏移下仍落在账单所属月、年，不表示
15 日发生了这些费用。已用真实 HA Core 2026.9.3 验证 external import、月/年 delta 和
Energy validator，覆盖 **Asia/Shanghai、UTC、America/Los_Angeles、Pacific/Kiritimati、
Pacific/Pago_Pago**；其他 HA 版本未经验证。缺失账单仍表示未知。日视图只会在 anchor
对应的本地日期出现稀疏月度点，不是实际日电费。不会按平均、比例、插值、补零或
tariff × daily kWh 制造日电费。最近结算日费用、本月费用仍然 unavailable；
production 不调用旧 daily-cost / yesterday API。集成不写 `.storage/energy`，
不调用 Energy preferences API 自动配置；请手动配对同账户的 energy / cost ID。

早期实验性 M6 的月初 anchor rows 不会自动搬移或清除。如已有 cost statistic 包含
这些 rows，Bridge 会报告 unexpected-row anomaly 并保留数据；该 statistic 恢复
按新约定收敛前，需要另行决定迁移方式。

## 显式广州当前电价配置

在 **选项 → 配置当前电价** 中选择广州缴费号，按已向南网登记的政策选择计费方式、
一户多人口和峰谷分时选项。旧 entry 和新 entry 没有用户确认的选择时，都保持
**unconfigured**，不再根据 `area_code == 080000` 自动认定普通单户。其他区域也保持
未配置。日电量、正式账单、energy statistic 和 official cost statistic 独立正常工作。

| 计费方式 | 多人口 | 峰谷 | 当前展示 |
| --- | --- | --- | --- |
| 未配置 | 关闭 | 关闭 | 阶梯、剩余额度和单价 unavailable |
| 阶梯 | 关闭 | 关闭 | 普通一户一表阶梯 |
| 阶梯 | 开启 | 关闭 | 第一、第二档阈值顺延 100 kWh |
| 阶梯 | 关闭 | 开启 | 当前分时基础价 + 阶梯加价 |
| 阶梯 | 开启 | 开启 | 多人口阈值及峰谷价 |
| 合表 | 关闭 | 关闭 | 固定 0.62586875 CNY/kWh；阶梯和剩余 unavailable |

不支持合表 + 峰谷、合表 + 多人口附加选项。7 人及以上已登记合表计价的家庭，
直接选择合表。Reauth 保留选择，新添加账户默认未配置。配置存于 config entry
settings，不进入 HistoryStore，仅描述**当前**政策；policy effective date 不参与
历史 bill 计算，也不能倒推出账户历史资格或选择。不做 derived cost 对账或本月估算。

| 政策 | 第一档上限 | 第二档上限 |
| --- | --- | --- |
| 普通户，5–10 月 | 260 kWh | 600 kWh |
| 普通户，11–4 月 | 200 kWh | 400 kWh |
| 多人口，5–10 月 | 360 kWh | 700 kWh |
| 多人口，11–4 月 | 300 kWh | 500 kWh |

阈值包含上限。普通三档全口径单价为 **0.58886875 / 0.63886875 / 0.88886875 CNY/kWh**。
峰谷第一档全口径单价为 **峰 0.99500875、平 0.58886875、谷 0.22914475**。
Decimal component model 为 **0.5802 × 时段比价 + 0.00866875 CNY/kWh**：基础价不含
政府性基金及附加，固定附加部分不参与比价。
先分时，后阶梯：第二档在分时基础价加 **0.05**，第三档加 **0.30**；居民不实施尖峰价。
不会把包含基金及附加的平段价直接乘峰段比价。数值累计及阈值比较使用 `Decimal(str(value))`。

上海时段为谷 **00:00–08:00**，峰 **10:00–12:00、14:00–19:00**，其余为平。
当前单价 sensor 保留原 `current_ladder_tariff` unique ID，单位改为 **CNY/kWh**，
不使用 MONETARY device class 或 TOTAL 状态类。峰谷边界从缓存电量本地刷新，不额外
请求 daily API。仅当月初至最新 observed/published date 每天都有合法、有限、非负的
kWh 时，才展示确定的阶梯起始日。daily client 将已出现日期但读数无效的行保留为
date-only coverage marker，因此 invalid tail 不会缩短覆盖区间。缺日或无效读数使
start date unavailable，但仍可用权威 `usage_total` 判断档位和剩余额度。marker 没有
kWh，HistoryStore 不将其保存为 fact；无效昨日读数保持 unavailable，最新结算日仅从
有效 daily fact rows 中选择。

静态政策来源：粤价〔2012〕135号（阶梯）、粤发改价格〔2017〕498号（广州价格）、
粤发改价格函〔2021〕826号 / 粤发改价格函〔2023〕553号（多人口）、
粤发改价格〔2021〕331号（峰谷）。
[2021 政策公告](https://www.ndrc.gov.cn/xwdt/gdzt/jgjzgg/nyjggg/202110/t20211027_1301148.html)
明确将旧 **1.65 : 1 : 0.5** 调整为 **1.7 : 1 : 0.38**，自 **2021-10-01** 执行。
[2024 广州政策解读](https://www.haizhu.gov.cn/zwgk/zdlyxxgk/jghsf/jgbz/content/post_9476644.html)
及其 [政策正文](https://www.haizhu.gov.cn/gzhzfg/attachment/7/7570/7570103/9476645.pdf)
继续确认比价不含政府性基金及附加，并确认先分时、后阶梯。
[2019 广州价目表](https://www.gz.gov.cn/attachment/0/89/89550/6432486.pdf)
仅用于普通阶梯及合表价格参考，不将其中旧峰谷价格作为现行峰谷政策。
运行时不抓取政府网页。

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
已配置的峰谷单价还会在上海峰谷时段边界本地刷新。

## API 实现

[`custom_components/csg/csg_client/__init__.py`](custom_components/csg/csg_client/__init__.py) 实现了南网 App API，也可独立使用。基本示例见 `csg_client_demo.py`。

## 致谢

- [CubicPill/china_southern_power_grid_stat](https://github.com/CubicPill/china_southern_power_grid_stat)，上游项目。
- [lyylyylyylyy](https://github.com/lyylyylyylyy)，上游短信验证码登录支持。
