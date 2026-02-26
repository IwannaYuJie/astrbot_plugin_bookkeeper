# astrbot_plugin_bookkeeper

AI-assisted bookkeeping plugin for AstrBot.

## Features

- **AI Auto Bookkeeping**: Detects spending facts via LLM tool-calling
- **Manual Query**: Daily, monthly, and custom date range bills
- **Category Summary**: Aggregated statistics by expense category
- **Record Management**: Delete records by index
- **Whitelist**: Sender whitelist with optional admin bypass
- **Scheduled Reports**: Daily/monthly bill push at configured times
- **Timezone Support**: Configurable IANA timezone

## Installation

1. Place plugin folder under AstrBot plugin path:

```text
AstrBot/data/plugins/astrbot_plugin_bookkeeper
```

2. Required files:

- `main.py`
- `metadata.yaml`
- `_conf_schema.json`
- `requirements.txt`

3. Start AstrBot and reload plugin in dashboard.

## Commands

Use `book` or alias `bk`.

### Query

- `book today` — Today's bill
- `book month` — This month's bill
- `book range <start> <end>` — Custom date range (YYYY-MM-DD)
- `book summary` — Monthly category summary

### Record Management

- `book del <index>` — Delete today's record by index
- `book del month <index>` — Delete this month's record by index

### Admin Commands

- `book auto <on|off>` — Toggle AI auto bookkeeping
- `book daily <on|off> [HH:MM]` — Daily scheduled bill
- `book monthly <on|off> [DAY] [HH:MM]` — Monthly scheduled bill
- `book tz <timezone|system>` — Set timezone
- `book status` — View plugin status

### Whitelist (Admin)

- `book wl on|off` — Toggle whitelist
- `book wl add <user_id>` — Add to whitelist
- `book wl del <user_id>` — Remove from whitelist
- `book wl ls` — List whitelist

## Configuration

See `_conf_schema.json` for full schema.

| Key                      | Type   | Default | Description                  |
| ------------------------ | ------ | ------- | ---------------------------- |
| `auto_extract_enabled`   | bool   | true    | AI auto bookkeeping toggle   |
| `whitelist_enabled`      | bool   | false   | Whitelist check toggle       |
| `whitelist_admin_bypass` | bool   | true    | Allow admin bypass           |
| `whitelist_user_ids`     | list   | []      | Allowed sender IDs           |
| `currency_symbol`        | string | 元      | Currency label in reports    |
| `max_records`            | int    | 5000    | Max stored records           |
| `max_report_items`       | int    | 100     | Max items per report         |
| `daily_report_enabled`   | bool   | false   | Daily report toggle          |
| `daily_report_time`      | string | 21:30   | Daily report time (HH:MM)    |
| `monthly_report_enabled` | bool   | false   | Monthly report toggle        |
| `monthly_report_day`     | int    | 1       | Monthly report day (1-31)    |
| `monthly_report_time`    | string | 21:30   | Monthly report time          |
| `schedule_timezone`      | string | ""      | IANA timezone (empty=system) |

## Limitations

- Auto extraction quality depends on your LLM/tool-calling capability.
- Only explicit spending facts should be recorded.
- If network/platform sending fails, scheduled push may not reach users.

## Development

```bash
ruff format .
ruff check .
```
