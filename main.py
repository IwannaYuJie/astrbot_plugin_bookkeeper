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
    """AI-assisted bookkeeping with tool auto-recording, whitelist, and schedules."""

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
        """Bookkeeping commands."""

    @book.command("help")
    async def book_help(self, event: AstrMessageEvent) -> None:
        help_text = "\n".join(
            [
                "Bookkeeper commands:",
                "book today",
                "book month",
                "book auto <on|off>                     (admin)",
                "book daily <on|off> [HH:MM]            (admin)",
                "book monthly <on|off> [DAY] [HH:MM]    (admin)",
                "book tz <IANA timezone|system>         (admin)",
                "book status                            (admin)",
                "book wl on|off                         (admin)",
                "book wl add <user_id>                  (admin)",
                "book wl del <user_id>                  (admin)",
                "book wl ls                             (admin)",
            ]
        )
        yield event.plain_result(help_text)

    @book.command("today")
    async def book_today(self, event: AstrMessageEvent) -> None:
        if not self._is_user_allowed(event):
            yield event.plain_result("Whitelist rejected this sender.")
            return
        today = self._today_local()
        records = await self._query_records_for_session(
            event.unified_msg_origin,
            today,
            today + timedelta(days=1),
        )
        yield event.plain_result(
            self._render_bill("Daily bill", today.isoformat(), records)
        )

    @book.command("month")
    async def book_month(self, event: AstrMessageEvent) -> None:
        if not self._is_user_allowed(event):
            yield event.plain_result("Whitelist rejected this sender.")
            return
        today = self._today_local()
        start, end = self._month_range(today)
        records = await self._query_records_for_session(
            event.unified_msg_origin, start, end
        )
        period = f"{start.isoformat()} to {(end - timedelta(days=1)).isoformat()}"
        yield event.plain_result(self._render_bill("Monthly bill", period, records))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @book.command("status")
    async def book_status(self, event: AstrMessageEvent) -> None:
        yield event.plain_result(self._status_text())

    @filter.permission_type(filter.PermissionType.ADMIN)
    @book.command("auto")
    async def book_auto(self, event: AstrMessageEvent, enabled: str = "") -> None:
        switch = self._parse_switch(enabled)
        if switch is None:
            yield event.plain_result("Usage: book auto <on|off>")
            return
        self.config["auto_extract_enabled"] = switch
        self._save_config()
        yield event.plain_result(f"auto_extract_enabled set to {switch}.")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @book.command("daily")
    async def book_daily(
        self,
        event: AstrMessageEvent,
        enabled: str = "",
        report_time: str = "",
    ) -> None:
        switch = self._parse_switch(enabled)
        if switch is None:
            yield event.plain_result("Usage: book daily <on|off> [HH:MM]")
            return

        if report_time:
            if not self._parse_hhmm(report_time):
                yield event.plain_result("Invalid time. Expected HH:MM.")
                return
            self.config["daily_report_time"] = report_time

        self.config["daily_report_enabled"] = switch
        self._save_config()
        await self._sync_cron_jobs()
        yield event.plain_result(
            f"daily_report_enabled={switch}, daily_report_time={self._cfg_str('daily_report_time', '21:30')}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @book.command("monthly")
    async def book_monthly(
        self,
        event: AstrMessageEvent,
        enabled: str = "",
        arg1: str = "",
        arg2: str = "",
    ) -> None:
        switch = self._parse_switch(enabled)
        if switch is None:
            yield event.plain_result("Usage: book monthly <on|off> [DAY] [HH:MM]")
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
                    yield event.plain_result("Invalid day. Expected integer 1-31.")
                    return

        if arg2:
            report_time = arg2

        if day < 1 or day > 31:
            yield event.plain_result("Invalid day. Expected range 1-31.")
            return
        if not self._parse_hhmm(report_time):
            yield event.plain_result("Invalid time. Expected HH:MM.")
            return

        self.config["monthly_report_enabled"] = switch
        self.config["monthly_report_day"] = day
        self.config["monthly_report_time"] = report_time
        self._save_config()
        await self._sync_cron_jobs()
        yield event.plain_result(
            f"monthly_report_enabled={switch}, monthly_report_day={day}, monthly_report_time={report_time}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @book.command("tz")
    async def book_timezone(
        self, event: AstrMessageEvent, timezone_name: str = ""
    ) -> None:
        if not timezone_name:
            current = self._cfg_str("schedule_timezone", "") or "system"
            yield event.plain_result(f"Current schedule timezone: {current}")
            return
        if timezone_name.lower() == "system":
            self.config["schedule_timezone"] = ""
            self._save_config()
            await self._sync_cron_jobs()
            yield event.plain_result("schedule_timezone reset to system timezone.")
            return
        if not self._is_valid_timezone(timezone_name):
            yield event.plain_result(
                "Invalid timezone. Use IANA timezone like Asia/Shanghai."
            )
            return
        self.config["schedule_timezone"] = timezone_name
        self._save_config()
        await self._sync_cron_jobs()
        yield event.plain_result(f"schedule_timezone set to {timezone_name}")

    @book.group("wl")
    def whitelist(self) -> None:
        """Whitelist management."""

    @filter.permission_type(filter.PermissionType.ADMIN)
    @whitelist.command("on")
    async def wl_on(self, event: AstrMessageEvent) -> None:
        self.config["whitelist_enabled"] = True
        self._save_config()
        yield event.plain_result("whitelist_enabled set to True.")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @whitelist.command("off")
    async def wl_off(self, event: AstrMessageEvent) -> None:
        self.config["whitelist_enabled"] = False
        self._save_config()
        yield event.plain_result("whitelist_enabled set to False.")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @whitelist.command("add")
    async def wl_add(self, event: AstrMessageEvent, user_id: str = "") -> None:
        if not user_id:
            yield event.plain_result("Usage: book wl add <user_id>")
            return
        whitelist_ids = self._get_whitelist_ids()
        if user_id in whitelist_ids:
            yield event.plain_result(f"user_id {user_id} already exists in whitelist.")
            return
        whitelist_ids.append(user_id)
        self.config["whitelist_user_ids"] = whitelist_ids
        self._save_config()
        yield event.plain_result(f"user_id {user_id} added to whitelist.")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @whitelist.command("del")
    async def wl_del(self, event: AstrMessageEvent, user_id: str = "") -> None:
        if not user_id:
            yield event.plain_result("Usage: book wl del <user_id>")
            return
        whitelist_ids = self._get_whitelist_ids()
        if user_id not in whitelist_ids:
            yield event.plain_result(f"user_id {user_id} is not in whitelist.")
            return
        whitelist_ids = [uid for uid in whitelist_ids if uid != user_id]
        self.config["whitelist_user_ids"] = whitelist_ids
        self._save_config()
        yield event.plain_result(f"user_id {user_id} removed from whitelist.")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @whitelist.command("ls")
    async def wl_list(self, event: AstrMessageEvent) -> None:
        whitelist_ids = self._get_whitelist_ids()
        if not whitelist_ids:
            yield event.plain_result("Whitelist is empty.")
            return
        lines = ["Whitelist IDs:"] + [
            f"{idx}. {uid}" for idx, uid in enumerate(whitelist_ids, start=1)
        ]
        yield event.plain_result("\n".join(lines))

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
                return False, "Bookkeeping skipped: duplicated tool call."
            records.append(record)
            max_records = max(self._cfg_int("max_records", 5000), 1)
            if len(records) > max_records:
                records = records[-max_records:]
            await self.put_kv_data(self.RECORDS_KEY, records)
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

    def _render_bill(
        self, title: str, period: str, records: list[dict[str, Any]]
    ) -> str:
        if not records:
            return f"{title}\nPeriod: {period}\nNo records."

        max_items = max(self._cfg_int("max_report_items", 100), 1)
        currency = self._cfg_str("currency_symbol", "CNY")
        lines = [title, f"Period: {period}"]
        total = 0.0

        for idx, record in enumerate(records[:max_items], start=1):
            amount = self._safe_float(record.get("amount"))
            sender_name = (record.get("sender_name") or "").strip()
            item = (record.get("item") or "unknown").strip()
            if sender_name:
                lines.append(f"{idx}. {item} - {amount:.2f} ({sender_name})")
            else:
                lines.append(f"{idx}. {item} - {amount:.2f}")
            total += amount

        if len(records) > max_items:
            lines.append(f"... and {len(records) - max_items} more records.")

        lines.append(f"Total: {total:.2f} {currency} ({len(records)} records)")
        return "\n".join(lines)

    def _status_text(self) -> str:
        timezone_name = self._cfg_str("schedule_timezone", "") or "system"
        whitelist_ids = self._get_whitelist_ids()
        return "\n".join(
            [
                "Bookkeeper status:",
                f"auto_extract_enabled={self._cfg_bool('auto_extract_enabled', True)}",
                f"whitelist_enabled={self._cfg_bool('whitelist_enabled', False)}",
                f"whitelist_admin_bypass={self._cfg_bool('whitelist_admin_bypass', True)}",
                f"whitelist_user_ids={len(whitelist_ids)}",
                f"daily_report_enabled={self._cfg_bool('daily_report_enabled', False)}",
                f"daily_report_time={self._cfg_str('daily_report_time', '21:30')}",
                f"monthly_report_enabled={self._cfg_bool('monthly_report_enabled', False)}",
                f"monthly_report_day={self._cfg_int('monthly_report_day', 1)}",
                f"monthly_report_time={self._cfg_str('monthly_report_time', '21:30')}",
                f"schedule_timezone={timezone_name}",
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
                "[Scheduled] Daily bill", target.isoformat(), session_records
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

        period = f"{start.isoformat()} to {(end - timedelta(days=1)).isoformat()}"
        for session, session_records in session_map.items():
            session_records.sort(key=lambda item: str(item.get("timestamp", "")))
            text = self._render_bill(
                "[Scheduled] Monthly bill", period, session_records
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
