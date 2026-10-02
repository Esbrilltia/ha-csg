# CSG Statistics Plus

[English](README.md) | [简体中文](README.zh-CN.md)

**CSG Plus** (`ha-csg-plus`) is an independent Home Assistant custom integration
for China Southern Power Grid electricity data. **Home Assistant domain: `csg_plus`.**
Candidate version: **`3.0.0-beta.1`**.

This project builds on [orangeboyChen/ha-csg](https://github.com/orangeboyChen/ha-csg)
and [CubicPill/china_southern_power_grid_stat](https://github.com/CubicPill/china_southern_power_grid_stat).
It is a community project with no official endorsement from Home Assistant or
China Southern Power Grid. It can coexist temporarily with upstream `csg`;
configuration, registry identities, private storage and Recorder sources are separate.

## Features

- Supports accounts in the China Southern Power Grid service area: Guangdong,
  Guangxi, Yunnan, Guizhou, and Hainan.
- Supports SMS, SMS plus password, CSG App QR code, WeChat QR code, and Alipay
  QR code login.
- Supports multiple CSG accounts and multiple payment accounts per CSG account.
- Uses Home Assistant's UI configuration flow; YAML configuration is not supported.
- Keeps the full payment account number in device and entity names.

## Data freshness

The current production path uses published daily usage and official billing snapshots.

| Data | Source | Freshness |
| --- | --- | --- |
| Balance and arrears | `queryUserAccountNumberSurplus` | Latest account snapshot |
| Yesterday and recent daily usage | `get_month_daily_usage_detail()` / `queryDayElectricByMPoint` | Published daily facts; an unpublished yesterday is unavailable |
| Current tariff and ladder | Explicit current Guangzhou tariff profile; authoritative current-month usage for ladder | Unconfigured until confirmed in Options; TOU follows Asia/Shanghai time |
| Previous settled month and year usage/cost | `get_year_month_stats()` / `getAnalyzeFeeDetails` | Official billing snapshots; current year is settled through the previous month |

Daily usage is not live power or today's accumulated consumption. The current
daily API returns kWh only, so latest settlement-day cost and this-month cost
stay unavailable without an authoritative charge. Previous-month and year
cost snapshots retain their existing official sources; no daily cost is inferred.

## Entities

Each payment account provides the following sensors:

- Yesterday usage, balance, and arrears.
- Current ladder tier, remaining energy, and tariff.
- Latest settlement-day usage and cost.
- This month, last month, this year, and last year usage and cost totals.

Snapshots use `measurement` or no state class. The integration no longer
creates **Energy total** or **Settled cost total**. There is no replacement
`total_increasing` sensor.

## External energy statistics

The supported Energy consumption path is:

```text
CSG daily facts → HistoryStore → EnergyStatisticsBridge
→ Home Assistant Recorder external statistics → Energy dashboard
```

**Options → Settings → Enable external energy and cost statistics** defaults to **on**.
Entries with no setting and new entries are enabled by default.
An explicit **off** is preserved and performs no integration
Recorder reads or writes. Each payment account has a stable
`csg_plus:energy_<full SHA-256>` ID and a non-sensitive `CSG Plus energy <8 hex digits>` name.
The full SHA-256 is derived from the payment account number.

The integration does not modify Energy dashboard preferences. For each account:

1. Confirm that the new external statistic has been generated.
2. Manually switch its electricity consumption source to `csg_plus:energy_<full SHA-256>`.
3. With Home Assistant currency **CNY**, choose `csg_plus:cost_<the same full SHA-256>`
   as the consumption source's tracked cost (`stat_cost`).
4. Do not configure both the old Energy total and the new external statistic as
   consumption sources for the same account; that would double-count usage.

The old Energy total and Settled cost total are retired. Home Assistant may
retain unavailable/restored registry placeholders. Existing entity registry
entries and Recorder history/statistics are never automatically deleted.
CSG Plus only uses `csg_plus.history_store.<entry_id>`. Both
`csg.energy_ledger.<entry_id>` and `csg.history_store.<entry_id>` belong to the
other domain and are never read, changed, migrated or deleted by CSG Plus.
Old `csg:energy_*` and `csg:cost_*` statistics remain separate and untouched.
Any later cleanup is a separate user operation. The retired ledger,
interpolation and correction path remains inactive; Settled cost total is not
a supported cost source.

Statistics use each published day's actual kWh, including real zero, at midnight
in `Asia/Shanghai`. Missing days stay absent. There is one daily aggregate point,
without invented hourly distribution; hourly charts may therefore look sparse.
Monthly bills and reconciliation never alter these daily statistics. Historical
revisions and newly published missing days converge on later syncs or restart.
Queued imports are read back before a final comparison with the latest durable
facts. Unconfirmed import ownership survives an integration reload in memory;
temporary Recorder failures defer convergence without discarding that ownership.
Ordinary unload stops producers before draining imports. Home Assistant shutdown
cancels the wait, and a fresh process compares against the committed database.
Turning the setting off stops Recorder access and retains existing statistics.
There is no destructive statistics cleanup feature.

## Official monthly cost statistics

The only cost authority is `getAnalyzeFeeDetails → get_year_month_stats()`
(`actualTotalAmount`) → `HistoryStore.monthly_bills[].cost_cny`. The same Bridge
materializes a separate `csg_plus:cost_<full SHA-256(account_number)>` statistic, named
`CSG Plus cost <first 8 hex digits>`. These statistics contain no account number,
customer name or address. Core 2026.9.3's Opower external-cost metadata is used:
`source=csg_plus`, `mean_type=NONE`, `has_sum=True`, `unit_class=None`, and
`unit_of_measurement=None`. Their monetary values are always **CNY**.

Home Assistant Energy uses one global currency. When it is not **CNY**, energy
usage statistics continue normally, but the integration performs **zero cost
Recorder reads or writes**, warns once per lifecycle, and retains any existing
cost statistic. Set the HA currency to CNY and reload to build or resume costs
from durable facts. There is no currency conversion. Changing or disabling a
tariff profile does not change or delete official costs. Disabling external
statistics stops both energy and cost access while retaining their history.

CSG bill months are natural calendar months in **Asia/Shanghai**. Only months
before the current Shanghai month are materialized, even if the upstream API
sends a current-month bill. Each whole-month fact is anchored at **12:00
Asia/Shanghai on its 15th day**, then HA converts it to UTC. `state`
is that month's official cost; `sum` accumulates known bills using Decimal.
Real zero is valid. Missing, invalid or unpublished months have no row. Filling
a hole or revising a bill, including a downward revision, rebuilds the cumulative
suffix from the earliest changed month. No delta adjustment or clearing is used.
Cost imports use the same per-statistic ownership, real readback, retry, unload
producer barrier and fresh-process recovery as energy imports, in separate lanes.
Each official billing response with accepted month candidates requests Bridge
convergence after all upserts, including unchanged refetches and a swallowed save failure.
The Bridge can retry pending persistence through its durable gate; once facts
and Recorder agree, repeated refreshes perform no additional imports.

**Official costs are monthly only.** HA charts and calendar aggregation use the
configured HA timezone. The mid-month anchor is a Recorder materialization
convention that keeps each cost in its bill's month and year across IANA offsets;
it is not a charge incurred on the 15th. Real HA Core 2026.9.3 import, monthly and
yearly deltas, and Energy validation are tested with **Asia/Shanghai, UTC,
America/Los_Angeles, Pacific/Kiritimati and Pacific/Pago_Pago**. Other HA versions
are unverified. Missing bills remain unknown. Day views show sparse points on
the local dates containing those anchors, not actual daily charges. No daily charge is allocated,
interpolated, filled with zero or reconstructed with tariff × daily kWh. Latest
settlement-day cost and this-month cost remain unavailable. Production never
calls the retired daily-cost or yesterday API wrappers. The integration does
not write `.storage/energy` or call Energy preferences APIs; users pair the
consumption and cost IDs manually.

Unexpected rows within an existing `csg_plus` cost statistic, including earlier
experimental month-start anchors, are not moved or cleared. The Bridge reports
an anomaly and preserves them. Old-domain cost statistics are never inspected
or migrated by CSG Plus.

## Explicit current Guangzhou tariff profiles

In **Options → Configure current tariff**, choose a Guangzhou payment account
and the billing scheme, registered multi-person allowance and registered TOU
setting. Both new and existing accounts without a confirmed choice remain
**unconfigured**; area code `080000` does not imply ordinary single-household
billing. Other regions remain unconfigured. Daily facts, official bills and
both external statistics work independently of this selection.

| Billing scheme | Multi-person | TOU | Current display |
| --- | --- | --- | --- |
| Unconfigured | Off | Off | Tier, remaining and tariff unavailable |
| Ladder | Off | Off | Ordinary household tier and rate |
| Ladder | On | Off | Thresholds increased by 100 kWh |
| Ladder | Off | On | Current TOU base rate plus ladder surcharge |
| Ladder | On | On | Multi-person thresholds and TOU rate |
| Combined | Off | Off | Fixed 0.62586875 CNY/kWh; no ladder or remaining |

Combined + TOU or combined + multi-person is unsupported. A family of seven or
more registered for combined billing selects combined directly. Reauth preserves
selections; each added account starts unconfigured. Selections live in config
entry settings, never in HistoryStore, and only describe the **current** policy.
The profile's policy effective date does not reconstruct an account's historical
eligibility or apply to official bills. There is no derived-cost reconciliation
or running monthly estimate.

| Policy | First-tier limit | Second-tier limit |
| --- | --- | --- |
| Ordinary, May–October | 260 kWh | 600 kWh |
| Ordinary, November–April | 200 kWh | 400 kWh |
| Multi-person, May–October | 360 kWh | 700 kWh |
| Multi-person, November–April | 300 kWh | 500 kWh |

Limits are inclusive. Ordinary inclusive rates are **0.58886875**, **0.63886875**
and **0.88886875 CNY/kWh**. First-tier TOU inclusive rates are **peak 0.99500875**,
**flat 0.58886875**, **valley 0.22914475**. The Decimal component model is
**0.5802 × period ratio + 0.00866875 CNY/kWh**: the base excludes government
funds/addons, and the fixed addons do not take part in the ratio. TOU adds
**0.05** in tier 2 or **0.30**
in tier 3 after the time-of-use rate; residential users have no sharp-peak rate.
The inclusive flat price is never multiplied by a peak ratio. All calculations
and threshold comparisons use `Decimal(str(value))`.

Asia/Shanghai periods are valley **00:00–08:00**, peak **10:00–12:00** and
**14:00–19:00**, flat otherwise. The current tariff sensor keeps its original
`current_ladder_tariff` suffix under `csg_plus.<account>.<suffix>` and reports **CNY/kWh**, without a monetary
device class or total state class. TOU boundaries update locally from cached
usage, without additional daily API calls. A ladder start date is shown only
when every date from month start through the newest observed/published day has
valid finite nonnegative kWh. The daily client preserves dated invalid readings
as date-only coverage markers, so an invalid tail cannot shorten that interval.
Holes or invalid readings leave the start date unavailable while the
authoritative total still determines tier and remaining allowance. Markers have
no kWh: HistoryStore does not save them as facts, invalid yesterday remains
unavailable, and latest settlement selects only valid daily fact rows.

Static policy sources are 粤价〔2012〕135号 (ladder), 粤发改价格〔2017〕498号
(Guangzhou prices), 粤发改价格函〔2021〕826号 and 粤发改价格函〔2023〕553号
(multi-person), and 粤发改价格〔2021〕331号 (TOU). The
[2021 policy announcement](https://www.ndrc.gov.cn/xwdt/gdzt/jgjzgg/nyjggg/202110/t20211027_1301148.html)
changes the old **1.65 : 1 : 0.5** ratio to **1.7 : 1 : 0.38**, effective
**2021-10-01**. The
[2024 Guangzhou explanation](https://www.haizhu.gov.cn/zwgk/zdlyxxgk/jghsf/jgbz/content/post_9476644.html)
and its [policy text](https://www.haizhu.gov.cn/gzhzfg/attachment/7/7570/7570103/9476645.pdf)
confirm that these ratios exclude government funds/addons and that TOU comes
before ladder surcharges. The
[2019 Guangzhou price table](https://www.gz.gov.cn/attachment/0/89/89550/6432486.pdf)
is a reference for ordinary and combined prices; its old TOU entries are not
the current TOU policy. Government websites are not queried at runtime.

## Installation

The audited baseline is Home Assistant Core **`2026.9.3`** / Python **`3.14.2`**.
HACS declares **`2026.9.3`** as the minimum HA version. Other Core versions have
not been tested; no wider compatibility is claimed.

After a standalone build is published, add this repository to
[HACS](https://hacs.xyz/) as a custom repository with category **Integration**,
then install **CSG Statistics Plus** and restart Home Assistant. The package
contains one integration: `custom_components/csg_plus`.

The current repository is [Esbrilltia/ha-csg](https://github.com/Esbrilltia/ha-csg).
The intended canonical repository is
[Esbrilltia/ha-csg-plus](https://github.com/Esbrilltia/ha-csg-plus/), with
[its issue tracker](https://github.com/Esbrilltia/ha-csg-plus/issues).
Those final URLs may not be available until the separately authorized repository
rename after M7 audit. This milestone prepares `3.0.0-beta.1` packaging and does
not publish a tag, release or HACS beta.

## Moving from upstream csg

`csg_plus` is a new integration. It does not rename or migrate an existing
`csg` config entry. Login, tariff profile and history options must be configured
again. The same payment account can temporarily exist in both domains without
overwriting devices, entities, Stores or statistics. New devices are named
`CSG Plus Account-...`; the manufacturer remains `CSG`. Similar entity names may
receive an automatic Home Assistant entity-ID suffix.

1. Install **CSG Statistics Plus** through HACS once its standalone build is available.
2. Add the new **CSG Statistics Plus** (`csg_plus`) integration, sign in again,
   and select the required tariff profile and history options.
3. Confirm login and retrieval of real China Southern Power Grid data work.
4. Confirm the entities display normally and are not all unavailable.
5. Confirm `csg_plus:energy_*` statistics have been generated.
6. Manually switch Energy dashboard consumption to the new energy statistic.
7. If using official costs, set HA currency to **CNY** and select `csg_plus:cost_*`.
8. Once the new sources work, remove the old `csg` integration.

**Do not remove old `csg` while Energy dashboard still references its Energy
total or other old consumption source.** Long-term parallel operation is not
required. Avoid configuring both sources for the same usage. CSG Plus never
writes Energy preferences or automatically cleans up old data.

## Update intervals

Balance, arrears, ladder state, and yesterday usage refresh at the configured
interval (four hours by default). Daily billing details, monthly summaries, and
yearly summaries refresh once per day.
Configured TOU prices also refresh at their Shanghai time boundaries.

## Historical fact backfill

In **Options → Settings**, set **Historical sync start month** to a calendar
month in `YYYY-MM` format. This is the earliest month you permit the integration
to fetch, shared by all payment accounts in that config entry. It does not
claim that the API has data back to that month. Leave it empty to disable new
backfill; existing facts and progress are retained. Upgrades do not enable it
automatically.

Each entry load starts one background pass through the previous calendar month
in `Asia/Shanghai`. Daily usage is requested by month and official bills by year,
sequentially, with independent progress for each account and lane. Confirmed
units are skipped after restart or reload; failed units are retried on the next
load. Extending the range fetches new units, including newly covered months
within an already requested bill year. There is no periodic historical rescan.

Facts and reconciliation are verified as persisted before each checkpoint is
saved and verified. Missing readings stay missing, and reconciliation only
compares complete daily coverage with official monthly usage. These historical
facts do not change snapshot sensor sources. A completed history pass requests
Bridge convergence for both daily usage and official monthly costs.

## API implementation

[`custom_components/csg_plus/csg_client/__init__.py`](custom_components/csg_plus/csg_client/__init__.py)
implements the CSG App API and can be used independently. See
`csg_client_demo.py` for a basic example.

## Credits

- [orangeboyChen/ha-csg](https://github.com/orangeboyChen/ha-csg), the upstream `csg` integration and v2 foundation.
- [CubicPill/china_southern_power_grid_stat](https://github.com/CubicPill/china_southern_power_grid_stat), the upstream project.
- [lyylyylyylyy](https://github.com/lyylyylyylyy), for upstream SMS verification-code login support.

The original authors' contributions and the existing [GPL-3.0 license](LICENSE)
are retained. The unchanged `brand/icon.png` (256 × 256) and `brand/icon@2x.png`
(512 × 512) come from [home-assistant/brands at commit
792a7f45a882bc5f3a01b661acdfd3bba4ac98e4](https://github.com/home-assistant/brands/tree/792a7f45a882bc5f3a01b661acdfd3bba4ac98e4/custom_integrations/china_southern_power_grid_stat),
under `custom_integrations/china_southern_power_grid_stat`.
Their Git blob SHAs are `e62fa5453c7119bd7b0987b101558c8a2f2c564b` and
`3ce6c56969aa36f96f0a82bce4808201662cbe7d`, respectively. Reusing this historical
integration icon does not imply endorsement by Home Assistant or China Southern Power Grid.
