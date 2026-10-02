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

## Energy statistics and M5 migration

The supported Energy consumption path is:

```text
CSG daily facts → HistoryStore → EnergyStatisticsBridge
→ Home Assistant Recorder external statistics → Energy dashboard
```

**Options → Settings → Enable external energy and cost statistics** defaults to **on**.
Existing entries with no setting are enabled on upgrade, and new entries are
enabled by default. An explicit **off** is preserved and performs no integration
Recorder reads or writes. Each payment account has a stable
`csg:energy_<full SHA-256>` ID and a non-sensitive `CSG energy <8 hex digits>` name.
The full SHA-256 is derived from the payment account number.

The integration does not modify Energy dashboard preferences. For each account:

1. Confirm that the new external statistic has been generated.
2. Manually switch its electricity consumption source to `csg:energy_<full SHA-256>`.
3. With Home Assistant currency **CNY**, choose `csg:cost_<the same full SHA-256>`
   as the consumption source's tracked cost (`stat_cost`).
4. Do not configure both the old Energy total and the new external statistic as
   consumption sources for the same account; that would double-count usage.

The old Energy total and Settled cost total are retired. Home Assistant may
retain unavailable/restored registry placeholders. Existing entity registry
entries and Recorder history/statistics are never automatically deleted.
The old `csg.energy_ledger.<entry_id>` Store remains on disk as inert legacy
data: it is not loaded, saved, migrated, or removed. Any later cleanup is a
separate user operation.

M6 adds official monthly cost statistics and explicit current tariff profiles.
Its fixed base is the M5 merge `6c193acb50fe365c3adb4c40a040bb2b2a449ea1`.
The retired ledger/interpolation/correction path remains inactive; Settled cost
total is not a supported cost source.

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
materializes a separate `csg:cost_<full SHA-256(account_number)>` statistic, named
`CSG cost <first 8 hex digits>`. These statistics contain no account number,
customer name or address. Core 2026.9.3's Opower external-cost metadata is used:
`source=csg`, `mean_type=NONE`, `has_sum=True`, `unit_class=None`, and
`unit_of_measurement=None`. Their monetary values are always **CNY**.

Home Assistant Energy uses one global currency. When it is not **CNY**, energy
usage statistics continue normally, but the integration performs **zero cost
Recorder reads or writes**, warns once per lifecycle, and retains any existing
cost statistic. Set the HA currency to CNY and reload to build or resume costs
from durable facts. There is no currency conversion. Changing or disabling a
tariff profile does not change or delete official costs. Disabling external
statistics stops both energy and cost access while retaining their history.

Only months before the current Asia/Shanghai month are materialized, even if
the upstream API sends a current-month bill. Each known month's interval starts
at **00:00 Asia/Shanghai on its first day**, then HA converts it to UTC. `state`
is that month's official cost; `sum` accumulates known bills using Decimal.
Real zero is valid. Missing, invalid or unpublished months have no row. Filling
a hole or revising a bill, including a downward revision, rebuilds the cumulative
suffix from the earliest changed month. No delta adjustment or clearing is used.
Cost imports use the same per-statistic ownership, real readback, retry, unload
producer barrier and fresh-process recovery as energy imports, in separate lanes.

**Official costs are monthly only.** Monthly totals are correct for known bills;
missing bills remain unknown. The day view can show sparse points on month
starts. This is intentional monthly bill behavior. No daily charge is allocated,
interpolated, filled with zero or reconstructed with tariff × daily kWh. Latest
settlement-day cost and this-month cost remain unavailable. Production never
calls the retired daily-cost or yesterday API wrappers. The integration does
not write `.storage/energy` or call Energy preferences APIs; users pair the
consumption and cost IDs manually.

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
and **0.88886875 CNY/kWh**. First-tier TOU inclusive rates are **peak 0.96596875**,
**flat 0.58886875**, **valley 0.29876875**. TOU adds **0.05** in tier 2 or **0.30**
in tier 3 after the time-of-use rate; residential users have no sharp-peak rate.
The inclusive flat price is never multiplied by a peak ratio. All calculations
and threshold comparisons use `Decimal(str(value))`.

Asia/Shanghai periods are valley **00:00–08:00**, peak **10:00–12:00** and
**14:00–19:00**, flat otherwise. The current tariff sensor keeps its original
`current_ladder_tariff` unique ID and now reports **CNY/kWh**, without a monetary
device class or total state class. TOU boundaries update locally from cached
usage, without additional daily API calls. A ladder start date is shown only
when every date from month start through the newest published day is present;
holes leave that date unavailable while the authoritative total still determines
tier and remaining allowance.

Static policy sources are 粤价〔2012〕135号 (ladder), 粤发改价格〔2017〕498号
(Guangzhou prices), 粤发改价格函〔2021〕826号 and 粤发改价格函〔2023〕553号
(multi-person), and 粤发改价格〔2021〕331号 (TOU). Rates are cross-checked against
the [official Guangzhou inclusive price table](https://www.gz.gov.cn/attachment/0/89/89550/6432486.pdf);
the [official policy explanation](https://www.haizhu.gov.cn/gzhzfg/attachment/7/7570/7570103/9476645.pdf)
confirms thresholds and TOU-before-ladder calculation. Government websites are
not queried at runtime.

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

[`custom_components/csg/csg_client/__init__.py`](custom_components/csg/csg_client/__init__.py)
implements the CSG App API and can be used independently. See
`csg_client_demo.py` for a basic example.

## Credits

- [CubicPill/china_southern_power_grid_stat](https://github.com/CubicPill/china_southern_power_grid_stat), the upstream project.
- [lyylyylyylyy](https://github.com/lyylyylyylyy), for upstream SMS verification-code login support.
