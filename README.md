# astrbot_plugin_bookkeeper

AstrBot 的 AI 智能记账插件。

## 功能说明

- **AI 自动记账**：通过 LLM 工具调用自动识别消息中的消费事实并记账
- **手动查询**：支持查询当日、当月、自定义日期范围的账单
- **分类汇总**：按消费项分类统计金额和笔数
- **记录管理**：支持删除指定序号的记录
- **白名单管理**：支持白名单开关、管理员绕过、用户增删查
- **定时推送**：支持每日/每月定时发送账单汇总
- **时区配置**：支持自定义 IANA 时区

## 安装

1. 将插件目录放到 AstrBot 插件路径：

```text
AstrBot/data/plugins/astrbot_plugin_bookkeeper
```

2. 确认目录内包含：

- `main.py`
- `metadata.yaml`
- `_conf_schema.json`
- `requirements.txt`

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
book summary
book range 2026-01-01 2026-01-31
```

### 5) 删除误录记录

```text
book today              ← 先查看序号
book del 3              ← 删除今日第 3 条
book del month 5        ← 删除本月第 5 条
```

## 命令列表

可用主命令：`book`（别名：`bk`）

### 查询类

- `book today` — 查看今日账单
- `book month` — 查看本月账单
- `book range <起始> <结束>` — 查看指定日期范围账单（YYYY-MM-DD）
- `book summary` — 查看本月分类汇总

### 记录管理

- `book del <序号>` — 删除今日指定记录
- `book del month <序号>` — 删除本月指定记录

### 管理命令（需管理员权限）

- `book auto <on|off>` — AI 自动记账开关
- `book daily <on|off> [HH:MM]` — 每日定时账单
- `book monthly <on|off> [天] [HH:MM]` — 每月定时账单
- `book tz <时区|system>` — 设置时区
- `book status` — 查看插件状态

### 白名单管理（需管理员权限）

- `book wl on|off` — 白名单开关
- `book wl add <用户ID>` — 添加白名单
- `book wl del <用户ID>` — 移除白名单
- `book wl ls` — 查看白名单

## 配置项

配置文件结构由 `_conf_schema.json` 定义。

| 配置项                   | 类型   | 默认值 | 说明                     |
| ------------------------ | ------ | ------ | ------------------------ |
| `auto_extract_enabled`   | bool   | true   | AI 自动记账开关          |
| `whitelist_enabled`      | bool   | false  | 白名单校验开关           |
| `whitelist_admin_bypass` | bool   | true   | 管理员是否绕过白名单     |
| `whitelist_user_ids`     | list   | []     | 白名单用户 ID 列表       |
| `currency_symbol`        | string | 元     | 账单货币标签             |
| `max_records`            | int    | 5000   | 最大存储记录数           |
| `max_report_items`       | int    | 100    | 单次账单最多展示条数     |
| `daily_report_enabled`   | bool   | false  | 每日定时账单开关         |
| `daily_report_time`      | string | 21:30  | 每日推送时间（HH:MM）    |
| `monthly_report_enabled` | bool   | false  | 每月定时账单开关         |
| `monthly_report_day`     | int    | 1      | 每月推送日（1-31）       |
| `monthly_report_time`    | string | 21:30  | 每月推送时间（HH:MM）    |
| `schedule_timezone`      | string | ""     | IANA 时区（空=系统时区） |

## AI 工具行为

工具名：`bookkeeper_add_expense`

参数：

- `item`（`string`）：简短支出项目描述
- `amount`（`number`）：支出金额，必须 `> 0`
- `note`（`string`，可选）

去重规则：近期记录中 `session + source_message_id + item + amount` 相同则跳过。

## 限制说明

- 自动抽取质量依赖你当前使用的 LLM 及其工具调用能力。
- 仅建议记录"明确的消费事实"。
- 若平台发送失败，定时推送可能无法送达。

## 开发建议

在插件目录执行：

```bash
ruff format .
ruff check .
```
