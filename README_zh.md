# astrbot_plugin_bookkeeper

AstrBot 的 AI 记账插件。

插件能力：
- 通过 LLM 工具调用识别用户消息中的“消费事实”并自动记账。
- 只记录简要账目（`项目 + 金额`）。
- 支持白名单开关与白名单管理。
- 支持每日/每月定时发送账单汇总。
- 支持手动查询当日与当月账单。

## 功能说明

- AI 自动记账：
  - 插件在 `on_llm_request` 中注入工具调用策略。
  - 当用户消息中存在明确消费事实时，LLM 调用 `bookkeeper_add_expense` 记账。
  - 只记录支出，不记录收入/退款/计划消费。
- 手动查询：
  - 查询当天账单。
  - 查询当月账单。
- 白名单：
  - 白名单总开关。
  - 管理员可选绕过白名单。
  - 支持白名单用户 ID 的增删查。
- 定时账单：
  - 每日按 `HH:MM` 定时发送。
  - 每月按“几号 + HH:MM”定时发送。
  - 支持配置时区。

## 安装

1. 将插件目录放到 AstrBot 插件路径：

```text
AstrBot/data/plugins/astrbot_plugin_bookkeeper
```

2. 确认目录内包含：
- `main.py`
- `metadata.yaml`
- `_conf_schema.json`
- `README.md`
- `README_zh.md`

3. 启动 AstrBot 后，在面板插件管理中重载插件。

## 快速开始

### 1) 开启自动记账

```text
book auto on
```

### 2) （可选）开启白名单

```text
book wl on
book wl add <你的用户ID>
```

### 3) 开启定时账单

每日账单：

```text
book daily on 21:30
```

每月账单（每月 1 号 21:30）：

```text
book monthly on 1 21:30
```

### 4) 手动查询

```text
book today
book month
```

## 命令列表

可用主命令：`book`（别名：`bk`）

- `book help`
- `book today`
- `book month`
- `book auto <on|off>`（管理员）
- `book daily <on|off> [HH:MM]`（管理员）
- `book monthly <on|off> [DAY] [HH:MM]`（管理员）
- `book tz <IANA timezone|system>`（管理员）
- `book status`（管理员）
- `book wl on|off`（管理员）
- `book wl add <user_id>`（管理员）
- `book wl del <user_id>`（管理员）
- `book wl ls`（管理员）

## 配置项

配置文件结构由 `_conf_schema.json` 定义。

主要配置：

- `auto_extract_enabled`：开启/关闭 AI 自动记账。
- `whitelist_enabled`：开启/关闭白名单校验。
- `whitelist_admin_bypass`：管理员是否绕过白名单。
- `whitelist_user_ids`：允许使用插件的用户 ID 列表。
- `currency_symbol`：账单输出的货币标签。
- `max_records`：最大存储记录数，超过后裁剪最旧记录。
- `max_report_items`：单次账单消息最多展示条数。
- `daily_report_enabled` / `daily_report_time`
- `monthly_report_enabled` / `monthly_report_day` / `monthly_report_time`
- `schedule_timezone`：
  - 使用 IANA 时区，例如 `Asia/Shanghai`。
  - 为空表示系统时区。

## AI 工具行为

工具名：

```text
bookkeeper_add_expense
```

参数：
- `item`（`string`）：简短支出项目描述
- `amount`（`number`）：支出金额，必须 `> 0`
- `note`（`string`，可选）

每条记录字段：
- session
- sender_id
- sender_name
- item
- amount
- note
- date
- timestamp
- source_message_id

去重规则：
- 在近期记录中，若 `session + source_message_id + item + amount` 相同则跳过。

## 定时账单机制

插件通过 AstrBot cron manager 注册 basic job：
- 每日表达式：`MM HH * * *`
- 每月表达式：`MM HH DAY * *`

账单消息内容包含：
- 标题
- 统计周期
- 条目明细
- 总金额

## 限制说明

- 自动抽取质量依赖你当前使用的 LLM 及其工具调用能力。
- 仅建议记录“明确的消费事实”。
- 若平台发送失败，定时推送可能无法送达。

## 开发建议

在插件目录执行：

```bash
uv run --no-sync ruff format .
uv run --no-sync ruff check .
```

