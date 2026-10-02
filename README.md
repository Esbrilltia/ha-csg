# ha-csg

[English](README.md) | [简体中文](README.zh-CN.md)

Home Assistant custom integration for China Southern Power Grid electricity data.

This project is a fork of [CubicPill/china_southern_power_grid_stat](https://github.com/CubicPill/china_southern_power_grid_stat).
It uses the `csg` integration domain and redesigns the entities for correct Home
Assistant statistics and Energy dashboard use.

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
| Guangzhou ladder snapshot | Current-month daily usage | Existing single-household calculation; no tariff profile |
| Previous settled month cost and year usage/cost | `get_year_month_stats()` / `getAnalyzeFeeDetails` | Official billing snapshots; current year is settled through the previous month |

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

## Energy statistics and M5 migration

The supported Energy consumption path is:

```text
CSG daily facts → HistoryStore → EnergyStatisticsBridge
→ Home Assistant Recorder external statistics → Energy dashboard
```

**Options → Settings → Enable external energy statistics** defaults to **on**.
Existing entries with no setting are enabled on upgrade, and new entries are
enabled by default. An explicit **off** is preserved and performs no integration
Recorder reads or writes. Each payment account has a stable
`csg:energy_<full SHA-256>` ID and a non-sensitive `CSG energy <8 hex digits>` name.
The full SHA-256 is derived from the payment account number.

The integration does not modify Energy dashboard preferences. For each account:

1. Confirm that the new external statistic has been generated.
2. Manually switch its electricity consumption source to `csg:energy_<full SHA-256>`.
3. Do not configure both the old Energy total and the new external statistic as
   consumption sources for the same account; that would double-count usage.

The old Energy total and Settled cost total are retired. Home Assistant may
retain unavailable/restored registry placeholders. Existing entity registry
entries and Recorder history/statistics are never automatically deleted.
The old `csg.energy_ledger.<entry_id>` Store remains on disk as inert legacy
data: it is not loaded, saved, migrated, or removed. Any later cleanup is a
separate user operation.

There is no external cost statistic in M5. Settled cost total is not a supported
cost source. Tariff profiles, multi-person allowances, TOU, historical daily
cost derivation, and allocating monthly charges to days are deferred to M6.
No charge is calculated by multiplying current tariff by kWh.

The audited rollback target is the M4 merge
`e4af90b31e7a2ee9902adbb8255ed3559e9d93a3`, retaining the
HistoryStore → Bridge path. It is not a recommendation to restore the retired
ledger/interpolation/correction path.

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
There is no deletion feature, external cost statistic, or tariff calculation.

## Installation

Install through [HACS](https://hacs.xyz/) or download a release from
[orangeboyChen/ha-csg](https://github.com/orangeboyChen/ha-csg/releases).

The current development and test baseline is Home Assistant Core `2026.9.3` / Python `3.14.2`. Other Home Assistant versions may work, but are unsupported and untested by this project.

## Breaking upgrade to v2

Version 2 changes the integration domain from
`china_southern_power_grid_stat` to `csg`. There is no automatic migration.

1. Remove the old integration.
2. Restart Home Assistant.
3. Add **CSG** again and configure the account.
4. Configure the external statistic as described in the M5 migration above.

This domain change is separate from M5 retirement; historical data cleanup is
not automatic.

## Update intervals

Balance, arrears, ladder state, and yesterday usage refresh at the configured
interval (four hours by default). Daily billing details, monthly summaries, and
yearly summaries refresh once per day.

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
facts do not change sensor data sources or publish Recorder statistics.

## API implementation

[`custom_components/csg/csg_client/__init__.py`](custom_components/csg/csg_client/__init__.py)
implements the CSG App API and can be used independently. See
`csg_client_demo.py` for a basic example.

## Credits

- [CubicPill/china_southern_power_grid_stat](https://github.com/CubicPill/china_southern_power_grid_stat), the upstream project.
- [lyylyylyylyy](https://github.com/lyylyylyylyy), for upstream SMS verification-code login support.
