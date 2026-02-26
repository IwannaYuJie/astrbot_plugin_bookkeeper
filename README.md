# astrbot_plugin_bookkeeper

AI-assisted bookkeeping plugin for AstrBot.

It can:
- Detect spending facts from user messages through LLM tool-calling.
- Store concise expense records (`item + amount`).
- Enforce sender whitelist rules.
- Send scheduled daily/monthly bill summaries.
- Support manual query commands for daily/monthly bills.

## Features

- AI auto bookkeeping:
  - The plugin injects tool policy in `on_llm_request`.
  - LLM calls `bookkeeper_add_expense` when user text contains explicit spending facts.
  - Only expense records are stored (no income/refund/planned spending).
- Manual query:
  - Query daily bill.
  - Query monthly bill.
- Whitelist:
  - Global whitelist switch.
  - Optional admin bypass.
  - Add/delete/list whitelist user IDs.
- Scheduled reports:
  - Daily report at configured `HH:MM`.
  - Monthly report at configured day + `HH:MM`.
  - Optional timezone configuration.

## Installation

1. Put this plugin folder under AstrBot plugin path:

```text
AstrBot/data/plugins/astrbot_plugin_bookkeeper
```

2. Ensure files exist:
- `main.py`
- `metadata.yaml`
- `_conf_schema.json`
- `README.md`

3. Start AstrBot and reload plugin in dashboard plugin manager.

## Quick Start

### 1) Enable auto bookkeeping

```text
book auto on
```

### 2) (Optional) Enable whitelist

```text
book wl on
book wl add <your_user_id>
```

### 3) Enable scheduled report

Daily:

```text
book daily on 21:30
```

Monthly (day 1, 21:30):

```text
book monthly on 1 21:30
```

### 4) Query bills manually

```text
book today
book month
```

## Commands

Use `book` or alias `bk`.

- `book help`
- `book today`
- `book month`
- `book auto <on|off>` (admin)
- `book daily <on|off> [HH:MM]` (admin)
- `book monthly <on|off> [DAY] [HH:MM]` (admin)
- `book tz <IANA timezone|system>` (admin)
- `book status` (admin)
- `book wl on|off` (admin)
- `book wl add <user_id>` (admin)
- `book wl del <user_id>` (admin)
- `book wl ls` (admin)

## Configuration

Config schema file: `_conf_schema.json`

Main options:

- `auto_extract_enabled`:
  - Enable/disable AI auto bookkeeping.
- `whitelist_enabled`:
  - Enable/disable whitelist check.
- `whitelist_admin_bypass`:
  - Allow admins to bypass whitelist.
- `whitelist_user_ids`:
  - Allowed sender ID list.
- `currency_symbol`:
  - Bill output currency label.
- `max_records`:
  - Max stored records; old records are trimmed.
- `max_report_items`:
  - Max lines shown per bill message.
- `daily_report_enabled` / `daily_report_time`
- `monthly_report_enabled` / `monthly_report_day` / `monthly_report_time`
- `schedule_timezone`:
  - IANA timezone like `Asia/Shanghai`.
  - Empty means system timezone.

## AI Tool Behavior

Tool name:

```text
bookkeeper_add_expense
```

Tool args:
- `item` (`string`): brief expense description
- `amount` (`number`): expense amount, must be `> 0`
- `note` (`string`, optional)

Store fields per record:
- session
- sender_id
- sender_name
- item
- amount
- note
- date
- timestamp
- source_message_id

Duplicate protection:
- Same `session + source_message_id + item + amount` in recent records is skipped.

## Scheduled Reports

The plugin uses AstrBot cron manager with basic jobs:
- Daily cron expression: `MM HH * * *`
- Monthly cron expression: `MM HH DAY * *`

Generated bill message includes:
- Title
- Period
- Itemized records
- Total amount

## Limitations

- Auto extraction quality depends on your LLM/tool-calling capability.
- Only explicit spending facts should be recorded.
- If network/platform sending fails, scheduled push may not reach users.

## Development

Recommended checks in plugin directory:

```bash
uv run --no-sync ruff format .
uv run --no-sync ruff check .
```

