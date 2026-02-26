from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from astrbot.api import AstrBotConfig, logger, star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.provider import ProviderRequest


class Main(star.Star):
    """AI 智能记账插件：支持自动记账、白名单管理、定时推送和手动查询。"""

    RECORDS_KEY = "records_v1"
    CRON_IDS_KEY = "cron_job_ids_v1"

    def __init__(
        self, context: star.Context, config: AstrBotConfig | None = None
    ) -> None:
        super().__init__(context, config=config)
        self.config = config or {}
        self._records_lock = asyncio.Lock()
        self._cron_lock = asyncio.Lock()

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        await self._sync_cron_jobs()

    async def terminate(self) -> None:
        async with self._cron_lock:
            await self._delete_registered_cron_jobs_unlocked()

    @filter.on_llm_request()
    async def on_llm_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        if not self._cfg_bool("auto_extract_enabled", True):
            return
        if not self._is_user_allowed(event):
            return
        if "bookkeeper_add_expense" in (req.system_prompt or ""):
            return

        today = self._today_local().isoformat()
        req.system_prompt = (req.system_prompt or "") + (
            "\n\n[Bookkeeping Tool Policy]\n"
            "You can call `bookkeeper_add_expense` to store expense items.\n"
            f"Today is {today}.\n"
            "If and only if the latest user message contains explicit spending facts with amounts, "
            "call the tool once per expense item.\n"
            "Each record must be brief: item + amount.\n"
            "Do not guess missing amounts.\n"
            "Do not record income, refunds, or planned future spending.\n"
        )

    @filter.llm_tool("bookkeeper_add_expense")
    async def bookkeeper_add_expense(
        self,
        event: AstrMessageEvent,
        item: str,
        amount: float,
        note: str = "",
    ) -> str:
        """Record one expense item.

        Args:
            item(string): Brief expense description.
            amount(number): Expense amount, must be greater than 0.
            note(string): Optional short note.
        """
        if not self._cfg_bool("auto_extract_enabled", True):
            return "Bookkeeping skipped: auto_extract_enabled is off."
        if not self._is_user_allowed(event):
            return "Bookkeeping skipped: sender is not allowed by whitelist."

        sender_id = (event.get_sender_id() or "").strip()
        session = (event.unified_msg_origin or "").strip()
        if not sender_id or not session:
            return "Bookkeeping skipped: missing sender or session."

        clean_item = self._normalize_item(item)
        if not clean_item:
            return "Bookkeeping skipped: item is empty."

        try:
            clean_amount = self._normalize_amount(amount)
        except ValueError as exc:
            return f"Bookkeeping skipped: invalid amount ({exc})."

        message_id = str(getattr(event.message_obj, "message_id", "") or "")
        ok, reason = await self._append_record(
            session=session,
            sender_id=sender_id,
            sender_name=(event.get_sender_name() or "").strip(),
            item=clean_item,
            amount=clean_amount,
            note=(note or "").strip(),
            source_message_id=message_id,
        )
        if not ok:
            return reason
        return f"Saved: {clean_item} {clean_amount:.2f}"

    @filter.command_group("book", alias={"bk"})
    def book(self) -> None:
        """记账命令组。"""

    @book.command("help")
    async def book_help(self, event: AstrMessageEvent) -> None:
        """显示所有可用的记账命令。"""
        help_text = "\n".join(
            [
                "📒 记账助手命令列表：",
                "",
                "📊 查询类：",
                "  book today              - 查看今日账单",
                "  book month              - 查看本月账单",
                "  book range <起始> <结束> - 查看指定日期范围账单",
                "  book summary            - 查看本月分类汇总",
                "",
                "✏️ 记录管理：",
                "  book del <序号>          - 删除今日指定记录",
                "  book del month <序号>    - 删除本月指定记录",
                "",
                "⚙️ 管理命令（需管理员权限）：",
                "  book auto <on|off>                   - AI自动记账开关",
                "  book daily <on|off> [HH:MM]          - 每日定时账单",
                "  book monthly <on|off> [天] [HH:MM]   - 每月定时账单",
                "  book tz <时区|system>                - 设置时区",
                "  book status                          - 查看插件状态",
                "",
                "👥 白名单管理（需管理员权限）：",
                "  book wl on|off                       - 白名单开关",
                "  book wl add <用户ID>                 - 添加白名单",
                "  book wl del <用户ID>                 - 移除白名单",
                "  book wl ls                           - 查看白名单",
            ]
        )
        yield event.plain_result(help_text)

    @book.command("today")
    async def book_today(self, event: AstrMessageEvent) -> None:
        """查看今日账单。"""
        if not self._is_user_allowed(event):
            yield event.plain_result("⚠️ 白名单校验未通过，无法使用此功能。")
            return
        today = self._today_local()
        records = await self._query_records_for_session(
            event.unified_msg_origin,
            today,
            today + timedelta(days=1),
        )
        yield event.plain_result(
            self._render_bill("📅 今日账单", today.isoformat(), records)
        )

    @book.command("month")
    async def book_month(self, event: AstrMessageEvent) -> None:
        """查看本月账单。"""
        if not self._is_user_allowed(event):
            yield event.plain_result("⚠️ 白名单校验未通过，无法使用此功能。")
            return
        today = self._today_local()
        start, end = self._month_range(today)
        records = await self._query_records_for_session(
            event.unified_msg_origin, start, end
        )
        period = f"{start.isoformat()} 至 {(end - timedelta(days=1)).isoformat()}"
        yield event.plain_result(self._render_bill("📅 本月账单", period, records))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @book.command("status")
    async def book_status(self, event: AstrMessageEvent) -> None:
        """查看插件当前状态（管理员）。"""
        yield event.plain_result(self._status_text())

    @filter.permission_type(filter.PermissionType.ADMIN)
    @book.command("auto")
    async def book_auto(self, event: AstrMessageEvent, enabled: str = "") -> None:
        """开关 AI 自动记账功能（管理员）。"""
        switch = self._parse_switch(enabled)
        if switch is None:
            yield event.plain_result("用法：book auto <on|off>")
            return
        self.config["auto_extract_enabled"] = switch
        self._save_config()
        state = "开启" if switch else "关闭"
        yield event.plain_result(f"✅ AI 自动记账已{state}。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @book.command("daily")
    async def book_daily(
        self,
        event: AstrMessageEvent,
        enabled: str = "",
        report_time: str = "",
    ) -> None:
        """设置每日定时账单推送（管理员）。"""
        switch = self._parse_switch(enabled)
        if switch is None:
            yield event.plain_result("用法：book daily <on|off> [HH:MM]")
            return

        if report_time:
            if not self._parse_hhmm(report_time):
                yield event.plain_result("❌ 时间格式无效，请使用 HH:MM 格式。")
                return
            self.config["daily_report_time"] = report_time

        self.config["daily_report_enabled"] = switch
        self._save_config()
        await self._sync_cron_jobs()
        state = "开启" if switch else "关闭"
        time_str = self._cfg_str('daily_report_time', '21:30')
        yield event.plain_result(f"✅ 每日账单已{state}，推送时间：{time_str}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @book.command("monthly")
    async def book_monthly(
        self,
        event: AstrMessageEvent,
        enabled: str = "",
        arg1: str = "",
        arg2: str = "",
    ) -> None:
        """设置每月定时账单推送（管理员）。"""
        switch = self._parse_switch(enabled)
        if switch is None:
            yield event.plain_result("用法：book monthly <on|off> [天] [HH:MM]")
            return

        day = self._cfg_int("monthly_report_day", 1)
        report_time = self._cfg_str("monthly_report_time", "21:30")

        if arg1:
            if ":" in arg1:
                report_time = arg1
            else:
                try:
                    day = int(arg1)
                except ValueError:
                    yield event.plain_result("❌ 天数无效，请输入 1-31 的整数。")
                    return

        if arg2:
            report_time = arg2

        if day < 1 or day > 31:
            yield event.plain_result("❌ 天数超出范围，请输入 1-31。")
            return
        if not self._parse_hhmm(report_time):
            yield event.plain_result("❌ 时间格式无效，请使用 HH:MM 格式。")
            return

        self.config["monthly_report_enabled"] = switch
        self.config["monthly_report_day"] = day
        self.config["monthly_report_time"] = report_time
        self._save_config()
        await self._sync_cron_jobs()
        state = "开启" if switch else "关闭"
        yield event.plain_result(
            f"✅ 每月账单已{state}，每月 {day} 号 {report_time} 推送"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @book.command("tz")
    async def book_timezone(
        self, event: AstrMessageEvent, timezone_name: str = ""
    ) -> None:
        """设置定时任务时区（管理员）。"""
        if not timezone_name:
            current = self._cfg_str("schedule_timezone", "") or "系统默认"
            yield event.plain_result(f"📍 当前时区：{current}")
            return
        if timezone_name.lower() == "system":
            self.config["schedule_timezone"] = ""
            self._save_config()
            await self._sync_cron_jobs()
            yield event.plain_result("✅ 时区已重置为系统默认时区。")
            return
        if not self._is_valid_timezone(timezone_name):
            yield event.plain_result(
                "❌ 无效时区，请使用 IANA 时区格式，例如 Asia/Shanghai。"
            )
            return
        self.config["schedule_timezone"] = timezone_name
        self._save_config()
        await self._sync_cron_jobs()
        yield event.plain_result(f"✅ 时区已设置为 {timezone_name}")

    @book.group("wl")
    def whitelist(self) -> None:
        """白名单管理命令组。"""

    @filter.permission_type(filter.PermissionType.ADMIN)
    @whitelist.command("on")
    async def wl_on(self, event: AstrMessageEvent) -> None:
        """开启白名单功能。"""
        self.config["whitelist_enabled"] = True
        self._save_config()
        yield event.plain_result("✅ 白名单已开启。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @whitelist.command("off")
    async def wl_off(self, event: AstrMessageEvent) -> None:
        """关闭白名单功能。"""
        self.config["whitelist_enabled"] = False
        self._save_config()
        yield event.plain_result("✅ 白名单已关闭。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @whitelist.command("add")
    async def wl_add(self, event: AstrMessageEvent, user_id: str = "") -> None:
        """添加用户到白名单。"""
        if not user_id:
            yield event.plain_result("用法：book wl add <用户ID>")
            return
        whitelist_ids = self._get_whitelist_ids()
        if user_id in whitelist_ids:
            yield event.plain_result(f"⚠️ 用户 {user_id} 已在白名单中。")
            return
        whitelist_ids.append(user_id)
        self.config["whitelist_user_ids"] = whitelist_ids
        self._save_config()
        yield event.plain_result(f"✅ 用户 {user_id} 已添加到白名单。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @whitelist.command("del")
    async def wl_del(self, event: AstrMessageEvent, user_id: str = "") -> None:
        """从白名单移除用户。"""
        if not user_id:
            yield event.plain_result("用法：book wl del <用户ID>")
            return
        whitelist_ids = self._get_whitelist_ids()
        if user_id not in whitelist_ids:
            yield event.plain_result(f"⚠️ 用户 {user_id} 不在白名单中。")
            return
        whitelist_ids = [uid for uid in whitelist_ids if uid != user_id]
        self.config["whitelist_user_ids"] = whitelist_ids
        self._save_config()
        yield event.plain_result(f"✅ 用户 {user_id} 已从白名单移除。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @whitelist.command("ls")
    async def wl_list(self, event: AstrMessageEvent) -> None:
        """查看当前白名单列表。"""
        whitelist_ids = self._get_whitelist_ids()
        if not whitelist_ids:
            yield event.plain_result("📋 白名单为空。")
            return
        lines = ["📋 白名单用户列表："] + [
            f"{idx}. {uid}" for idx, uid in enumerate(whitelist_ids, start=1)
        ]
        yield event.plain_result("\n".join(lines))

    # ==================== 记录管理命令 ====================

    @book.command("del")
    async def book_del(
        self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""
    ) -> None:
        """删除指定序号的记录。支持 'book del <序号>' 和 'book del month <序号>'。"""
        if not self._is_user_allowed(event):
            yield event.plain_result("⚠️ 白名单校验未通过，无法使用此功能。")
            return

        # 解析参数：book del <序号> 或 book del month <序号>
        is_monthly = False
        index_str = arg1

        if arg1.lower() == "month":
            is_monthly = True
            index_str = arg2

        if not index_str:
            yield event.plain_result("用法：book del <序号> 或 book del month <序号>")
            return

        try:
            index = int(index_str)
        except ValueError:
            yield event.plain_result("❌ 序号必须是整数。")
            return

        if index < 1:
            yield event.plain_result("❌ 序号必须大于 0。")
            return

        # 获取对应时间范围的记录
        today = self._today_local()
        if is_monthly:
            start, end = self._month_range(today)
            scope_label = "本月"
        else:
            start = today
            end = today + timedelta(days=1)
            scope_label = "今日"

        session = event.unified_msg_origin
        records = await self._query_records_for_session(session, start, end)

        if not records:
            yield event.plain_result(f"📋 {scope_label}暂无记录可删除。")
            return

        if index > len(records):
            yield event.plain_result(
                f"❌ 序号超出范围，{scope_label}共 {len(records)} 条记录。"
            )
            return

        # 找到要删除的记录并从全局记录中移除
        target_record = records[index - 1]
        deleted = await self._delete_record(target_record)

        if deleted:
            item = target_record.get("item", "未知")
            amount = self._safe_float(target_record.get("amount"))
            logger.info(f"bookkeeper: 记录已删除 - {item} {amount:.2f}")
            yield event.plain_result(
                f"✅ 已删除{scope_label}第 {index} 条记录：{item} - {amount:.2f}"
            )
        else:
            yield event.plain_result("❌ 删除失败，记录可能已被移除。")

    @book.command("summary")
    async def book_summary(self, event: AstrMessageEvent) -> None:
        """查看本月分类汇总统计。"""
        if not self._is_user_allowed(event):
            yield event.plain_result("⚠️ 白名单校验未通过，无法使用此功能。")
            return

        today = self._today_local()
        start, end = self._month_range(today)
        records = await self._query_records_for_session(
            event.unified_msg_origin, start, end
        )

        if not records:
            period = f"{start.isoformat()} 至 {(end - timedelta(days=1)).isoformat()}"
            yield event.plain_result(f"📊 本月分类汇总\n统计区间：{period}\n暂无记录。")
            return

        yield event.plain_result(self._render_summary(records, start, end))

    @book.command("range")
    async def book_range(
        self, event: AstrMessageEvent, start_str: str = "", end_str: str = ""
    ) -> None:
        """查看指定日期范围的账单。日期格式：YYYY-MM-DD。"""
        if not self._is_user_allowed(event):
            yield event.plain_result("⚠️ 白名单校验未通过，无法使用此功能。")
            return

        if not start_str or not end_str:
            yield event.plain_result(
                "用法：book range <起始日期> <结束日期>\n日期格式：YYYY-MM-DD"
            )
            return

        try:
            start_date = date.fromisoformat(start_str)
            end_date = date.fromisoformat(end_str)
        except ValueError:
            yield event.plain_result("❌ 日期格式无效，请使用 YYYY-MM-DD 格式。")
            return

        if start_date > end_date:
            yield event.plain_result("❌ 起始日期不能晚于结束日期。")
            return

        # 结束日期为 exclusive，所以 +1 天
        end_exclusive = end_date + timedelta(days=1)
        records = await self._query_records_for_session(
            event.unified_msg_origin, start_date, end_exclusive
        )
        period = f"{start_date.isoformat()} 至 {end_date.isoformat()}"
        yield event.plain_result(self._render_bill("📅 自定义日期账单", period, records))

    # ==================== 内部数据方法 ====================
    async def _append_record(
        self,
        *,
        session: str,
        sender_id: str,
        sender_name: str,
        item: str,
        amount: float,
        note: str,
        source_message_id: str,
    ) -> tuple[bool, str]:
        """追加一条记账记录，包含去重检查和容量限制。"""
        now = datetime.now(tz=self._effective_tz())
        today = now.date().isoformat()
        record = {
            "session": session,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "item": item,
            "amount": amount,
            "note": note,
            "date": today,
            "timestamp": now.isoformat(),
            "source_message_id": source_message_id,
        }

        async with self._records_lock:
            records = await self._load_records_unlocked()
            if self._is_duplicate_record(records, record):
                logger.debug(f"bookkeeper: 跳过重复记录 item={item} amount={amount}")
                return False, "记账跳过：重复的工具调用。"
            records.append(record)
            max_records = max(self._cfg_int("max_records", 5000), 1)
            if len(records) > max_records:
                trimmed = len(records) - max_records
                records = records[-max_records:]
                logger.info(
                    f"bookkeeper: 记录数量超过上限 {max_records}，已裁剪 {trimmed} 条旧记录"
                )
            await self.put_kv_data(self.RECORDS_KEY, records)
        logger.info(f"bookkeeper: 记录已保存 - {item} {amount:.2f} (sender={sender_id})")
        return True, "saved"

    async def _query_records_for_session(
        self,
        session: str,
        start_date: date,
        end_date_exclusive: date,
    ) -> list[dict[str, Any]]:
        async with self._records_lock:
            records = await self._load_records_unlocked()

        selected: list[dict[str, Any]] = []
        for record in records:
            if record.get("session") != session:
                continue
            record_date = self._record_date(record)
            if record_date is None:
                continue
            if start_date <= record_date < end_date_exclusive:
                selected.append(record)
        selected.sort(key=lambda item: str(item.get("timestamp", "")))
        return selected

    async def _get_records_snapshot(self) -> list[dict[str, Any]]:
        async with self._records_lock:
            return await self._load_records_unlocked()

    async def _load_records_unlocked(self) -> list[dict[str, Any]]:
        data = await self.get_kv_data(self.RECORDS_KEY, [])
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    def _is_duplicate_record(
        self, records: list[dict[str, Any]], record: dict[str, Any]
    ) -> bool:
        message_id = record.get("source_message_id", "")
        if not message_id:
            return False
        for old in reversed(records[-30:]):
            if (
                old.get("source_message_id") == message_id
                and old.get("session") == record.get("session")
                and old.get("item") == record.get("item")
                and self._safe_float(old.get("amount"))
                == self._safe_float(record.get("amount"))
            ):
                return True
        return False

    async def _delete_record(self, target: dict[str, Any]) -> bool:
        """从全局记录中删除指定的记录（通过 timestamp 精确匹配）。"""
        target_ts = target.get("timestamp", "")
        target_session = target.get("session", "")
        target_item = target.get("item", "")
        target_amount = self._safe_float(target.get("amount"))

        async with self._records_lock:
            records = await self._load_records_unlocked()
            original_len = len(records)
            # 通过 timestamp + session + item + amount 精确定位记录
            records = [
                r for r in records
                if not (
                    r.get("timestamp") == target_ts
                    and r.get("session") == target_session
                    and r.get("item") == target_item
                    and self._safe_float(r.get("amount")) == target_amount
                )
            ]
            if len(records) == original_len:
                return False
            await self.put_kv_data(self.RECORDS_KEY, records)
        return True

    def _render_summary(
        self,
        records: list[dict[str, Any]],
        start: date,
        end: date,
    ) -> str:
        """渲染按分类汇总的统计文本。"""
        period = f"{start.isoformat()} 至 {(end - timedelta(days=1)).isoformat()}"
        currency = self._cfg_str("currency_symbol", "元")

        # 按 item 名称分类汇总
        category_map: dict[str, dict[str, float | int]] = {}
        total = 0.0
        for record in records:
            item = (record.get("item") or "未知").strip()
            amount = self._safe_float(record.get("amount"))
            if item not in category_map:
                category_map[item] = {"amount": 0.0, "count": 0}
            category_map[item]["amount"] += amount
            category_map[item]["count"] += 1
            total += amount

        # 按金额降序排列
        sorted_categories = sorted(
            category_map.items(),
            key=lambda x: x[1]["amount"],
            reverse=True,
        )

        lines = ["📊 本月分类汇总", f"统计区间：{period}", ""]
        for idx, (item, stats) in enumerate(sorted_categories, start=1):
            amount = stats["amount"]
            count = int(stats["count"])
            # 计算占比
            pct = (amount / total * 100) if total > 0 else 0
            lines.append(f"{idx}. {item} - {amount:.2f} ({count}笔, {pct:.1f}%)")

        lines.append("")
        lines.append(f"💰 合计：{total:.2f} {currency}（共 {len(records)} 笔）")
        return "\n".join(lines)

    def _render_bill(
        self, title: str, period: str, records: list[dict[str, Any]]
    ) -> str:
        """渲染账单消息文本。"""
        if not records:
            return f"{title}\n统计区间：{period}\n暂无记录。"

        max_items = max(self._cfg_int("max_report_items", 100), 1)
        currency = self._cfg_str("currency_symbol", "元")
        lines = [title, f"统计区间：{period}", ""]
        total = 0.0

        for idx, record in enumerate(records[:max_items], start=1):
            amount = self._safe_float(record.get("amount"))
            sender_name = (record.get("sender_name") or "").strip()
            item = (record.get("item") or "未知").strip()
            if sender_name:
                lines.append(f"{idx}. {item} - {amount:.2f} ({sender_name})")
            else:
                lines.append(f"{idx}. {item} - {amount:.2f}")
            total += amount

        if len(records) > max_items:
            lines.append(f"... 另有 {len(records) - max_items} 条记录未显示")

        lines.append("")
        lines.append(f"💰 合计：{total:.2f} {currency}（共 {len(records)} 笔）")
        return "\n".join(lines)

    def _status_text(self) -> str:
        """生成插件状态摘要文本。"""
        timezone_name = self._cfg_str("schedule_timezone", "") or "系统默认"
        whitelist_ids = self._get_whitelist_ids()
        auto_state = "✅ 开启" if self._cfg_bool('auto_extract_enabled', True) else "❌ 关闭"
        wl_state = "✅ 开启" if self._cfg_bool('whitelist_enabled', False) else "❌ 关闭"
        daily_state = "✅ 开启" if self._cfg_bool('daily_report_enabled', False) else "❌ 关闭"
        monthly_state = "✅ 开启" if self._cfg_bool('monthly_report_enabled', False) else "❌ 关闭"
        return "\n".join(
            [
                "📊 记账助手状态：",
                "",
                f"  AI 自动记账：{auto_state}",
                f"  白名单：{wl_state}",
                f"  白名单用户数：{len(whitelist_ids)}",
                f"  每日账单：{daily_state}，时间：{self._cfg_str('daily_report_time', '21:30')}",
                f"  每月账单：{monthly_state}，每月 {self._cfg_int('monthly_report_day', 1)} 号 {self._cfg_str('monthly_report_time', '21:30')}",
                f"  时区：{timezone_name}",
            ]
        )

    async def _sync_cron_jobs(self) -> None:
        async with self._cron_lock:
            cron_mgr = self.context.cron_manager
            if not cron_mgr:
                logger.warning("bookkeeper: cron manager is not available.")
                return

            await self._delete_registered_cron_jobs_unlocked()

            timezone_name = (
                self._cfg_str("schedule_timezone", "") or ""
            ).strip() or None
            cron_ids: dict[str, str] = {}

            if self._cfg_bool("daily_report_enabled", False):
                expr = self._build_daily_cron_expression(
                    self._cfg_str("daily_report_time", "21:30")
                )
                if expr:
                    job = await cron_mgr.add_basic_job(
                        name=f"{self.plugin_id}_daily_bill",
                        cron_expression=expr,
                        handler=self._cron_daily_bill,
                        description="Bookkeeper daily bill push",
                        timezone=timezone_name,
                        enabled=True,
                        persistent=False,
                    )
                    cron_ids["daily"] = job.job_id
                else:
                    logger.warning(
                        "bookkeeper: invalid daily_report_time, daily job skipped."
                    )

            if self._cfg_bool("monthly_report_enabled", False):
                expr = self._build_monthly_cron_expression(
                    self._cfg_int("monthly_report_day", 1),
                    self._cfg_str("monthly_report_time", "21:30"),
                )
                if expr:
                    job = await cron_mgr.add_basic_job(
                        name=f"{self.plugin_id}_monthly_bill",
                        cron_expression=expr,
                        handler=self._cron_monthly_bill,
                        description="Bookkeeper monthly bill push",
                        timezone=timezone_name,
                        enabled=True,
                        persistent=False,
                    )
                    cron_ids["monthly"] = job.job_id
                else:
                    logger.warning(
                        "bookkeeper: invalid monthly schedule settings, monthly job skipped."
                    )

            await self.put_kv_data(self.CRON_IDS_KEY, cron_ids)

    async def _delete_registered_cron_jobs_unlocked(self) -> None:
        cron_mgr = self.context.cron_manager
        if not cron_mgr:
            return

        raw = await self.get_kv_data(self.CRON_IDS_KEY, {})
        job_ids: list[str] = []
        if isinstance(raw, dict):
            for value in raw.values():
                if isinstance(value, str) and value:
                    job_ids.append(value)
        elif isinstance(raw, list):
            for value in raw:
                if isinstance(value, str) and value:
                    job_ids.append(value)

        for job_id in job_ids:
            try:
                await cron_mgr.delete_job(job_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    f"bookkeeper: ignore cron delete failure for {job_id}: {exc}"
                )

        await self.put_kv_data(self.CRON_IDS_KEY, {})

    async def _cron_daily_bill(self) -> None:
        target = self._today_local()
        records = await self._get_records_snapshot()
        session_map: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            record_date = self._record_date(record)
            session = str(record.get("session") or "").strip()
            if not session or record_date != target:
                continue
            session_map.setdefault(session, []).append(record)

        for session, session_records in session_map.items():
            session_records.sort(key=lambda item: str(item.get("timestamp", "")))
            text = self._render_bill(
                "🔔 每日账单推送", target.isoformat(), session_records
            )
            await self.context.send_message(session, MessageChain([Plain(text)]))

    async def _cron_monthly_bill(self) -> None:
        today = self._today_local()
        start, end = self._month_range(today)
        records = await self._get_records_snapshot()
        session_map: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            record_date = self._record_date(record)
            session = str(record.get("session") or "").strip()
            if not session or record_date is None:
                continue
            if not (start <= record_date < end):
                continue
            session_map.setdefault(session, []).append(record)

        period = f"{start.isoformat()} 至 {(end - timedelta(days=1)).isoformat()}"
        for session, session_records in session_map.items():
            session_records.sort(key=lambda item: str(item.get("timestamp", "")))
            text = self._render_bill(
                "🔔 每月账单推送", period, session_records
            )
            await self.context.send_message(session, MessageChain([Plain(text)]))

    def _build_daily_cron_expression(self, report_time: str) -> str | None:
        hm = self._parse_hhmm(report_time)
        if not hm:
            return None
        hour, minute = hm
        return f"{minute} {hour} * * *"

    def _build_monthly_cron_expression(self, day: int, report_time: str) -> str | None:
        if day < 1 or day > 31:
            return None
        hm = self._parse_hhmm(report_time)
        if not hm:
            return None
        hour, minute = hm
        return f"{minute} {hour} {day} * *"

    def _is_user_allowed(self, event: AstrMessageEvent) -> bool:
        if not self._cfg_bool("whitelist_enabled", False):
            return True
        if self._cfg_bool("whitelist_admin_bypass", True) and event.is_admin():
            return True
        sender_id = (event.get_sender_id() or "").strip()
        if not sender_id:
            return False
        return sender_id in self._get_whitelist_ids()

    def _get_whitelist_ids(self) -> list[str]:
        data = (
            self.config.get("whitelist_user_ids", [])
            if isinstance(self.config, dict)
            else []
        )
        if not isinstance(data, list):
            return []
        return [str(item).strip() for item in data if str(item).strip()]

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = (
            self.config.get(key, default) if isinstance(self.config, dict) else default
        )
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            parsed = self._parse_switch(value)
            if parsed is not None:
                return parsed
        return default

    def _cfg_str(self, key: str, default: str) -> str:
        value = (
            self.config.get(key, default) if isinstance(self.config, dict) else default
        )
        if value is None:
            return default
        return str(value)

    def _cfg_int(self, key: str, default: int) -> int:
        value = (
            self.config.get(key, default) if isinstance(self.config, dict) else default
        )
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _save_config(self) -> None:
        if isinstance(self.config, AstrBotConfig):
            self.config.save_config()

    def _parse_switch(self, value: str | bool | None) -> bool | None:
        if isinstance(value, bool):
            return value
        if value is None:
            return None
        normalized = str(value).strip().lower()
        if normalized in {"on", "true", "1", "yes", "enable", "enabled"}:
            return True
        if normalized in {"off", "false", "0", "no", "disable", "disabled"}:
            return False
        return None

    def _parse_hhmm(self, raw_time: str) -> tuple[int, int] | None:
        parts = raw_time.strip().split(":")
        if len(parts) != 2:
            return None
        try:
            hour = int(parts[0])
            minute = int(parts[1])
        except ValueError:
            return None
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            return None
        return hour, minute

    def _normalize_item(self, item: str) -> str:
        clean = " ".join((item or "").strip().split())
        return clean[:80]

    def _normalize_amount(self, amount: float | int | str) -> float:
        try:
            decimal_amount = Decimal(str(amount))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("not a number") from exc
        if decimal_amount <= 0:
            raise ValueError("must be greater than 0")
        try:
            decimal_amount = decimal_amount.quantize(Decimal("0.01"))
        except InvalidOperation as exc:
            raise ValueError("invalid precision") from exc
        return float(decimal_amount)

    def _safe_float(self, value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _record_date(self, record: dict[str, Any]) -> date | None:
        raw_date = record.get("date")
        if not isinstance(raw_date, str):
            return None
        try:
            return date.fromisoformat(raw_date)
        except ValueError:
            return None

    def _today_local(self) -> date:
        return datetime.now(tz=self._effective_tz()).date()

    def _month_range(self, day: date) -> tuple[date, date]:
        start = day.replace(day=1)
        if start.month == 12:
            end = date(start.year + 1, 1, 1)
        else:
            end = date(start.year, start.month + 1, 1)
        return start, end

    def _effective_tz(self):
        timezone_name = (self._cfg_str("schedule_timezone", "") or "").strip()
        if timezone_name:
            try:
                return ZoneInfo(timezone_name)
            except Exception:  # noqa: BLE001
                logger.warning(
                    f"bookkeeper: invalid timezone {timezone_name}, fallback to system timezone."
                )
        return datetime.now().astimezone().tzinfo

    def _is_valid_timezone(self, timezone_name: str) -> bool:
        try:
            ZoneInfo(timezone_name)
            return True
        except Exception:  # noqa: BLE001
            return False
