from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any

from src.app.plugin_system.api import storage_api

from .client import NewAPIClient, NewAPIError


PLUGIN_NAME = "newapi_group_manager"
STATE_KEY = "state"


def _section(config: Any, name: str) -> Any:
    return getattr(config, name, None)


class NewAPIService:
    """Coordinates NewAPI calls and the plugin's persistent binding map."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.config

    def _newapi_config(self) -> Any:
        return _section(self.config, "newapi")

    def _group_config(self) -> Any:
        return _section(self.config, "group")

    def _new_client(self, token: str) -> NewAPIClient:
        cfg = self._newapi_config()
        if cfg is None:
            raise NewAPIError("NewAPI 配置缺失。")
        return NewAPIClient(
            str(getattr(cfg, "base_url", "")),
            token,
            timeout_seconds=int(getattr(cfg, "timeout_seconds", 10)),
            allow_insecure_http=bool(getattr(cfg, "allow_insecure_http", False)),
        )

    def _admin_client(self) -> NewAPIClient:
        cfg = self._newapi_config()
        env_name = str(getattr(cfg, "admin_token_env", "NEWAPI_ADMIN_TOKEN") or "").strip()
        admin_token = os.getenv(env_name, "").strip() if env_name else ""
        if not admin_token:
            raise NewAPIError(
                "管理员只读令牌尚未配置；请在容器环境变量中设置对应的 NewAPI PAT。"
            )
        return self._new_client(admin_token)

    def _state_lock(self) -> asyncio.Lock:
        lock = getattr(self.plugin, "_state_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            setattr(self.plugin, "_state_lock", lock)
        return lock

    async def _load_state(self) -> dict[str, Any]:
        data = await storage_api.load_json(PLUGIN_NAME, STATE_KEY)
        if not isinstance(data, dict):
            return {"bindings": {}}
        bindings = data.get("bindings")
        if not isinstance(bindings, dict):
            data["bindings"] = {}
        return data

    async def _save_state(self, state: dict[str, Any]) -> None:
        await storage_api.save_json(PLUGIN_NAME, STATE_KEY, state)

    async def get_binding(self, person_id: str) -> dict[str, str] | None:
        state = await self._load_state()
        record = state["bindings"].get(person_id)
        if not isinstance(record, dict):
            return None
        return {
            "user_id": str(record.get("user_id") or ""),
            "username": str(record.get("username") or ""),
        }

    async def bind_account(self, person_id: str, username: str) -> dict[str, str]:
        username = username.strip()
        if not person_id or not username:
            raise NewAPIError("请提供 NewAPI 用户名。")
        if len(username) > 128 or any(char.isspace() or ord(char) < 32 for char in username):
            raise NewAPIError("用户名不能包含空白字符，且长度不能超过 128 个字符。")

        normalized_username = username.casefold()
        profile = {
            "user_id": f"username:{normalized_username}",
            "username": username,
        }
        async with self._state_lock():
            state = await self._load_state()
            bindings: dict[str, Any] = state["bindings"]
            for other_person_id, record in bindings.items():
                if other_person_id == person_id or not isinstance(record, dict):
                    continue
                existing_username = str(record.get("username") or "").strip().casefold()
                existing_user_id = str(record.get("user_id") or "").strip().casefold()
                if existing_username == normalized_username or existing_user_id == profile["user_id"]:
                    raise NewAPIError("这个 NewAPI 用户名已经绑定到另一个 QQ 用户。")
            bindings[person_id] = profile
            await self._save_state(state)
        return profile

    async def unbind_account(self, person_id: str) -> bool:
        async with self._state_lock():
            state = await self._load_state()
            bindings: dict[str, Any] = state["bindings"]
            existed = bindings.pop(person_id, None) is not None
            if existed:
                await self._save_state(state)
            return existed

    def managed_group_ids(self) -> set[str]:
        cfg = self._group_config()
        values = getattr(cfg, "managed_group_ids", []) if cfg else []
        return {str(value).strip() for value in values if str(value).strip()}

    def gate_enabled(self) -> bool:
        cfg = self._group_config()
        return bool(getattr(cfg, "gate_enabled", False)) if cfg else False

    def post_join_kick_enabled(self) -> bool:
        cfg = self._group_config()
        return bool(getattr(cfg, "kick_unbound_after_join", False)) if cfg else False

    def _timezone(self) -> ZoneInfo:
        cfg = self._newapi_config()
        timezone = str(getattr(cfg, "timezone", "Asia/Shanghai")) if cfg else "Asia/Shanghai"
        try:
            return ZoneInfo(timezone)
        except ZoneInfoNotFoundError:
            return ZoneInfo("Asia/Shanghai")

    async def get_trend(
        self, person_id: str, days: int
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any] | None]:
        binding = await self.get_binding(person_id)
        if not binding or not binding["username"]:
            raise NewAPIError("你还没有绑定 NewAPI 账号。请私聊我使用 /api bind <用户名>。")

        client = self._admin_client()
        timezone = self._timezone()
        today = datetime.now(timezone).date()
        start_date = today - timedelta(days=days - 1)
        semaphore = asyncio.Semaphore(3)

        async def query_day(day: date) -> dict[str, Any]:
            start_dt = datetime.combine(day, time.min, timezone)
            end_dt = datetime.combine(day + timedelta(days=1), time.min, timezone) - timedelta(seconds=1)
            async with semaphore:
                values = await client.get_daily_usage(
                    binding["username"], int(start_dt.timestamp()), int(end_dt.timestamp())
                )
            return {"date": day, **values}

        period_start = datetime.combine(start_date, time.min, timezone)
        period_end = datetime.combine(today + timedelta(days=1), time.min, timezone) - timedelta(
            seconds=1
        )
        newapi_config = self._newapi_config()
        max_records = int(getattr(newapi_config, "max_latency_logs", 5000))
        usage_task = asyncio.create_task(
            client.get_usage_metrics(
                binding["username"],
                int(period_start.timestamp()),
                int(period_end.timestamp()),
                max_records=max_records,
            )
        )
        results = await asyncio.gather(
            *(query_day(start_date + timedelta(days=offset)) for offset in range(days))
        )
        try:
            usage_metrics = await usage_task
        except Exception:
            # Keep the core daily trend available if detailed logs are unavailable.
            usage_metrics = None
        return binding["username"], results, usage_metrics
