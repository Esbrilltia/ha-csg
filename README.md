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

The following classifications follow the upstream integration README and its API
mapping. They describe data freshness, not endpoint naming.

| Data | Source | Freshness |
| --- | --- | --- |
| Balance and arrears | `queryUserAccountNumberSurplus` | Realtime |
| Current ladder tier, remaining allowance, and tariff | `queryDayElectricChargeByMPoint` ladder fields | Realtime |
| Yesterday's electricity usage | `queryDayElectricByMPointYesterday` | Realtime |
| Daily usage, daily cost, monthly totals | `queryDayElectricByMPoint` and `queryDayElectricChargeByMPoint` | Delayed by about two days for the current month |
| Current and previous year totals | `getAnalyzeFeeDetails` | Non-realtime; current year is updated through the previous month |

“Realtime” yesterday usage is still a daily value. It is not live power or
today's accumulated consumption.

## Entities

Each payment account provides the following sensors:

- Energy total: cumulative `kWh` for the Home Assistant Energy dashboard.
- Settled cost total: cumulative `CNY` cost for Energy dashboard cost tracking.
- Yesterday usage, balance, and arrears.
- Current ladder tier, remaining energy, and tariff.
- Latest settlement-day usage and cost.
- This month, last month, this year, and last year usage and cost totals.

Only **Energy total** and **Settled cost total** use the
`total_increasing` state class. All query snapshots use `measurement` or no
state class, so Home Assistant does not mistake a monthly or yearly snapshot
for a continuously increasing meter.

## Energy dashboard and billing corrections

Energy total advances smoothly through the current day using the latest complete
daily reading as its rate. The ledger remains based on complete daily readings,
so this interpolation is only for the live entity value. When the delayed
daily bill becomes available, the integration compares its `result[].power` and
`result[].charge` values with stored data and corrects existing Home Assistant
Recorder statistics for that date.

The cumulative entities never decrease: historical bill corrections update the
Recorder's historical sums rather than resetting a `total_increasing` meter.
The first installation does not fabricate historical entity states from an old
bill. Cost is taken only from the settled daily `charge` value; the current
ladder tariff is never used to estimate cost.

To configure Home Assistant Energy using the existing entities, select:

- **Energy total** as the electricity consumption source.
- **Settled cost total** as the entity-with-total-cost source.

## Optional external energy statistics

Enable **Options → Settings → Enable external energy statistics** to publish
persisted HistoryStore daily usage as a new external energy source. The setting
defaults to off, including existing entries. Each payment account has a stable
`csg:energy_<full SHA-256>` ID and a non-sensitive `CSG energy <8 hex digits>` name.
Select that source manually as electricity consumption in the Energy dashboard.
The existing **Energy total** entity remains available; this does not automatically
migrate your dashboard or replace its configured sources.

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
4. Remove old devices, entities, and historical statistics if they are no longer needed.

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
