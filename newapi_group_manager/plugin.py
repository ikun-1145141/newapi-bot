from __future__ import annotations

import asyncio
import logging
import time
from typing import Annotated, Any

from src.app.plugin_system.api import adapter_api
from src.app.plugin_system.api.person_api import generate_person_id
from src.app.plugin_system.base import (
    BaseCommand,
    BaseEventHandler,
    BasePlugin,
    BaseTool,
    cmd_route,
    register_plugin,
)
from src.core.components.types import ChatType, EventType
from src.kernel.event import EventDecision

from .config import NewAPIGroupManagerConfig
from .chart import GatewayChartService
from .service import NewAPIService, PLUGIN_NAME


logger = logging.getLogger(__name__)


def _value(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _as_mapping(obj: Any) -> dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    for method_name in ("model_dump", "dict", "to_dict"):
        method = getattr(obj, method_name, None)
        if callable(method):
            try:
                value = method()
                if isinstance(value, dict):
                    return value
            except Exception:
                pass
    return {}


def _walk_mappings(root: Any, max_depth: int = 6):
    stack: list[tuple[Any, int]] = [(root, 0)]
    seen: set[int] = set()
    while stack:
        value, depth = stack.pop()
        if id(value) in seen or depth > max_depth:
            continue
        seen.add(id(value))
        mapping = _as_mapping(value)
        if mapping:
            yield mapping
            for child in mapping.values():
                if isinstance(child, (dict, list, tuple)) or hasattr(child, "__dict__"):
                    stack.append((child, depth + 1))
        elif isinstance(value, (list, tuple)):
            stack.extend((child, depth + 1) for child in value)


def _request_meta(message: Any) -> dict[str, Any] | None:
    for mapping in _walk_mappings(message):
        if mapping.get("is_request") is True and str(mapping.get("request_type") or "") == "group":
            return mapping
    return None


def _group_increase_meta(message: Any) -> dict[str, Any] | None:
    for mapping in _walk_mappings(message):
        raw = mapping.get("raw")
        candidates = [mapping]
        if isinstance(raw, dict):
            candidates.append(raw)
        for item in candidates:
            if str(item.get("notice_type") or "") == "group_increase":
                return item
    return None


def _is_private(ctx: Any) -> bool:
    for value in (getattr(ctx, "chat_type", None), getattr(ctx, "message_type", None)):
        if value is None:
            continue
        value = getattr(value, "value", value)
        if str(value).strip().lower() in {"private", "direct", "dm"}:
            return True
        if str(value).strip().lower() in {"group", "guild", "channel"}:
            return False
    message = getattr(ctx, "message", None) or ctx
    info = _value(message, "message_info")
    group_info = _value(info, "group_info") or _value(message, "group_info")
    if _value(group_info, "group_id") is not None:
        return False
    kind = (
        _value(info, "message_type")
        or _value(message, "message_type")
        or _value(message, "chat_type")
    )
    kind = getattr(kind, "value", kind)
    return str(kind or "").strip().lower() in {"private", "direct", "dm"}


def _person_id(ctx: Any) -> str:
    value = str(getattr(ctx, "person_id", "") or "").strip()
    if value:
        return value
    user_id = getattr(ctx, "user_id", None) or getattr(ctx, "sender_id", None)
    platform = getattr(ctx, "platform", None) or "qq"
    if user_id is None:
        message = getattr(ctx, "message", None) or ctx
        user_info = _value(_value(message, "message_info"), "user_info")
        user_info = user_info or _value(message, "user_info")
        user_id = _value(user_info, "user_id")
        platform = _value(user_info, "platform") or platform
    if user_id is None:
        return ""
    return generate_person_id(str(platform), str(user_id))


def _format_quota(value: int) -> str:
    return f"{value:,}"


def _text_from_segments(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "".join(_text_from_segments(item) for item in value)
    mapping = _as_mapping(value)
    if mapping:
        segment_type = str(mapping.get("type") or "").lower()
        data = mapping.get("data")
        if segment_type == "text":
            if isinstance(data, str):
                return data
            if isinstance(data, dict):
                return str(data.get("text") or "")
            return str(_value(data, "text", "") or "")
        if segment_type in {"seglist", "message", "message_segment"}:
            return _text_from_segments(data)
        if isinstance(data, (list, tuple)):
            return _text_from_segments(data)
    return ""


def _message_text(message: Any) -> str:
    for field in ("processed_plain_text", "plain_text", "text_description"):
        value = _value(message, field)
        if isinstance(value, str) and value.strip():
            return value
    for field in ("message_segment", "message_chain", "segments"):
        text = _text_from_segments(_value(message, field))
        if text:
            return text
    return ""


def _message_group_id(message: Any) -> str:
    info = _value(message, "message_info")
    group_info = _value(info, "group_info") or _value(message, "group_info")
    extra = _value(message, "extra")
    extra_group_info = _value(extra, "group_info")
    group_id = (
        _value(group_info, "group_id")
        or _value(extra_group_info, "group_id")
        or _value(extra, "group_id")
        or _value(message, "group_id")
    )
    if group_id is None:
        for mapping in _walk_mappings(message):
            if mapping.get("post_type") == "message" and mapping.get("message_type") == "group":
                group_id = mapping.get("group_id")
                if group_id is not None:
                    break
    return str(group_id or "").strip()


def _is_group_message(message: Any) -> bool:
    info = _value(message, "message_info")
    group_info = _value(info, "group_info") or _value(message, "group_info")
    if _value(group_info, "group_id") is not None:
        return True
    kind = _value(info, "message_type") or _value(message, "chat_type") or _value(message, "message_type")
    kind = getattr(kind, "value", kind)
    return str(kind or "").strip().lower() in {"group", "guild"}


def _is_self_message(message: Any) -> bool:
    info = _value(message, "message_info")
    user_info = _value(info, "user_info") or _value(message, "user_info")
    user_id = _value(user_info, "user_id") or _value(message, "sender_id")
    self_id = None
    for mapping in _walk_mappings(message):
        if mapping.get("self_id") is not None:
            self_id = mapping["self_id"]
            break
    return user_id is not None and self_id is not None and str(user_id) == str(self_id)


def _message_id(message: Any) -> str:
    info = _value(message, "message_info")
    value = _value(info, "message_id") or _value(message, "message_id")
    return str(value or "").strip()


def _pick_onebot_adapter() -> str | None:
    active = adapter_api.list_active_adapters()
    return next(
        (
            signature
            for signature in active
            if "onebot" in signature.lower() or "snowluma" in signature.lower()
        ),
        None,
    )


def _private_user_id(ctx: Any) -> str:
    user_id = _value(ctx, "user_id") or _value(ctx, "sender_id")
    if user_id is None:
        message = _value(ctx, "message") or ctx
        info = _value(message, "message_info")
        user_info = _value(info, "user_info") or _value(message, "user_info")
        user_id = (
            _value(user_info, "user_id")
            or _value(message, "sender_id")
            or _value(message, "user_id")
        )
    return str(user_id or "").strip()


def _forward_sender_id(ctx: Any) -> str:
    for mapping in _walk_mappings(ctx):
        value = mapping.get("self_id") or mapping.get("bot_id")
        if value is not None:
            return str(value).strip()
    return ""


def _chunk_lines(lines: list[str], max_chars: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in lines:
        remaining = line
        while len(remaining) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(remaining[:max_chars])
            remaining = remaining[max_chars:]
        candidate = f"{current}\n{remaining}" if current else remaining
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = remaining
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _adapter_command_succeeded(result: Any) -> bool:
    if not isinstance(result, dict) or result.get("status") != "ok":
        return False
    try:
        return int(result.get("retcode", 0) or 0) == 0
    except (TypeError, ValueError):
        return False


async def _send_private_forward(ctx: Any, lines: list[str]) -> bool:
    user_id = _private_user_id(ctx)
    adapter_sign = _pick_onebot_adapter()
    if not user_id or not user_id.isdigit() or not adapter_sign:
        return False

    sender_id = _forward_sender_id(ctx)
    if not sender_id:
        try:
            login_result = await adapter_api.send_adapter_command(
                adapter_sign=adapter_sign,
                command_name="get_login_info",
                command_data={},
                timeout=5.0,
            )
            login_data = login_result.get("data", {}) if isinstance(login_result, dict) else {}
            if isinstance(login_data, dict):
                sender_id = str(login_data.get("user_id") or "").strip()
        except Exception:
            logger.debug("Could not read OneBot self ID for a NewAPI trend forward")
    if not sender_id or not sender_id.isdigit():
        return False

    messages = [
        {
            "type": "node",
            "data": {
                "name": "NewAPI 调用趋势",
                "uin": sender_id,
                "content": [{"type": "text", "data": {"text": chunk}}],
            },
        }
        for chunk in _chunk_lines(lines, 1000)
    ]
    try:
        result = await adapter_api.send_adapter_command(
            adapter_sign=adapter_sign,
            command_name="send_private_forward_msg",
            command_data={"user_id": int(user_id), "messages": messages},
            timeout=15.0,
        )
    except Exception:
        logger.warning("NewAPI trend merged-forward send failed; falling back to text replies")
        return False
    if not _adapter_command_succeeded(result):
        logger.warning("NewAPI trend merged-forward was rejected; falling back to text replies")
        return False
    return True


async def _send_command_response(message: Any, response: str) -> bool:
    response = str(response or "").strip()
    if not response:
        return True

    adapter_sign = _pick_onebot_adapter()
    if not adapter_sign:
        logger.warning("Cannot reply to NewAPI command: no active OneBot adapter")
        return False

    if _is_group_message(message):
        group_id = _message_group_id(message)
        if not group_id.isdigit():
            logger.warning("Cannot reply to NewAPI command: group ID is missing")
            return False
        command_name = "send_group_msg"
        command_data = {"group_id": int(group_id), "message": response}
    elif _is_private(message):
        user_id = _private_user_id(message)
        if not user_id.isdigit():
            logger.warning("Cannot reply to NewAPI command: private user ID is missing")
            return False
        command_name = "send_private_msg"
        command_data = {"user_id": int(user_id), "message": response}
    else:
        logger.warning("Cannot reply to NewAPI command: unsupported chat type")
        return False

    try:
        result = await adapter_api.send_adapter_command(
            adapter_sign=adapter_sign,
            command_name=command_name,
            command_data=command_data,
            timeout=10.0,
        )
    except Exception:
        logger.exception("NewAPI command reply failed")
        return False
    if not _adapter_command_succeeded(result):
        logger.warning("NewAPI command reply was rejected by the OneBot adapter")
        return False
    return True


async def _send_private_text_chunks(message: Any, lines: list[str]) -> bool:
    user_id = _private_user_id(message)
    adapter_sign = _pick_onebot_adapter()
    if not user_id or not user_id.isdigit() or not adapter_sign:
        return False

    try:
        for chunk in _chunk_lines(lines, 1200):
            result = await adapter_api.send_adapter_command(
                adapter_sign=adapter_sign,
                command_name="send_private_msg",
                command_data={"user_id": int(user_id), "message": chunk},
                timeout=10.0,
            )
            if not _adapter_command_succeeded(result):
                logger.warning("NewAPI trend text chunk was rejected")
                return False
    except Exception:
        logger.warning("NewAPI trend text chunk send failed")
        return False
    return True


class NewAPIAccountStatusTool(BaseTool):
    """Look up the current private QQ user's NewAPI binding."""

    name = "newapi_account_status"
    description = (
        "检查当前私聊 QQ 用户是否已绑定 NewAPI 账号；只查询当前调用者，不接受其他人的 QQ 或 NewAPI 用户 ID。"
        "未绑定时提示用户通过 /api bind <用户名> 自行绑定，不要索取或处理任何密钥。"
    )
    chat_type = ChatType.PRIVATE

    async def execute(self) -> tuple[bool, dict[str, Any]]:
        message = self.trigger_message
        if message is None or not _is_private(message):
            return False, {"error": "此工具只支持私聊查询。"}
        person_id = _person_id(message)
        if not person_id:
            return False, {"error": "无法识别当前 QQ 身份。"}

        binding = await NewAPIService(self.plugin).get_binding(person_id)
        if binding is None:
            return True, {
                "bound": False,
                "next_step": "请用户私聊机器人使用 /api bind <NewAPI 用户名> 自行绑定。",
                "credential_handling": "绑定只使用用户名，不索取或处理密钥。",
            }
        return True, {"bound": True, "username": binding.get("username", "")}


class NewAPIUsageTrendTool(BaseTool):
    """Return the current private QQ user's usage and per-model latency."""

    name = "newapi_usage_trend"
    description = (
        "查询当前私聊 QQ 用户最近 7 天或 30 天的 NewAPI 调用趋势、额度、Token 和每个模型的延迟。"
        "只查询当前调用者；不得查询或猜测其他人的账号。"
    )
    chat_type = ChatType.PRIVATE

    async def execute(
        self,
        days: Annotated[int, "统计周期，只能是 7 或 30 天"] = 7,
    ) -> tuple[bool, dict[str, Any]]:
        if days not in (7, 30):
            return False, {"error": "统计周期只能选择 7 天或 30 天。"}
        message = self.trigger_message
        if message is None or not _is_private(message):
            return False, {"error": "此工具只支持私聊查询。"}
        person_id = _person_id(message)
        if not person_id:
            return False, {"error": "无法识别当前 QQ 身份。"}

        service = NewAPIService(self.plugin)
        try:
            username, daily_rows, metrics = await service.get_trend(person_id, days)
        except Exception as exc:
            return False, {"error": str(exc)}

        result: dict[str, Any] = {
            "username": username,
            "days": days,
            "total_requests": sum(row["requests"] for row in daily_rows),
            "total_quota": sum(row["quota"] for row in daily_rows),
            "daily": [
                {
                    "date": row["date"].isoformat(),
                    "requests": row["requests"],
                    "quota": row["quota"],
                }
                for row in daily_rows
            ],
        }
        if metrics is None:
            result["detailed_metrics_available"] = False
            result["metrics_note"] = "NewAPI 消费日志暂时无法读取。"
            return True, result

        result.update(
            {
                "detailed_metrics_available": True,
                "log_coverage": {
                    "sampled": metrics["records_sampled"],
                    "total": metrics["records_total"],
                    "partial": metrics["partial"],
                },
                "latency_seconds": {
                    "samples": metrics["latency_count"],
                    "average": metrics["avg_latency"],
                    "p95": metrics["p95_latency"],
                    "max": metrics["max_latency"],
                },
                "token_usage": {
                    "prompt": metrics["prompt_tokens"],
                    "completion": metrics["completion_tokens"],
                },
                "models": [
                    {
                        "model_name": model["model"],
                        "calls": model["calls"],
                        "latency_samples": model["latency_count"],
                        "average_latency_seconds": model["avg_latency"],
                        "p95_latency_seconds": model["p95_latency"],
                        "max_latency_seconds": model["max_latency"],
                        "prompt_tokens": model["prompt_tokens"],
                        "completion_tokens": model["completion_tokens"],
                    }
                    for model in metrics["models"]
                ],
            }
        )
        return True, result


class NewAPICommand(BaseCommand):
    name = "api"
    description = "绑定 NewAPI 账号并查询调用趋势"
    command_prefix = "/"

    def _service(self) -> NewAPIService:
        return NewAPIService(self.plugin)

    @staticmethod
    def _help_text() -> str:
        return (
            "NewAPI 命令帮助：\n"
            "/api help — 查看帮助\n"
            "/api bind <用户名> — 绑定你的 NewAPI 用户名（仅私聊）\n"
            "/api unbind — 解除绑定（仅私聊）\n"
            "/api trend [7|30] — 查询最近 7 天或 30 天趋势（仅私聊）"
        )

    async def execute(self, message_text: str) -> tuple[bool, str]:
        text = message_text.strip()
        if not text:
            response = self._help_text()
        else:
            known_routes = {
                "help", "帮助", "bind", "绑定", "unbind", "解绑",
                "trend", "趋势", "调用趋势",
            }
            if text.split(maxsplit=1)[0] not in known_routes:
                response = f"未知的 /api 子命令。发送 /api help 查看用法。\n\n{self._help_text()}"
            else:
                success, response = await super().execute(message_text)
                if not success:
                    response = f"{response}\n发送 /api help 查看用法。"

        await _send_command_response(self._message, response)
        return True, response

    @cmd_route("help")
    async def handle_help(self) -> tuple[bool, str]:
        return True, self._help_text()

    @cmd_route("帮助")
    async def handle_help_cn(self) -> tuple[bool, str]:
        return True, self._help_text()

    async def _reply_trend(self, lines: list[str]) -> tuple[bool, str]:
        report = "\n".join(lines)
        if len(report) <= 1200:
            return True, report
        if await _send_private_forward(self._message, lines):
            return True, "趋势报告较长，已通过合并消息发送。"
        if await _send_private_text_chunks(self._message, lines):
            return True, "趋势报告较长，已分段发送。"
        return True, report

    @cmd_route("bind")
    async def handle_bind(self, username: str) -> tuple[bool, str]:
        message = self._message
        if not _is_private(message):
            return True, "绑定只接受私聊。请私聊发送 /api bind <用户名>。"
        person_id = _person_id(message)
        if not person_id:
            return True, "无法识别你的 QQ 身份，请联系管理员处理。"
        try:
            profile = await self._service().bind_account(person_id, username)
        except Exception as exc:
            return True, f"绑定失败：{exc}\n发送 /api help 查看用法。"
        return True, f"已将你的 QQ 绑定到 NewAPI 用户「{profile['username']}」。"

    @cmd_route("绑定")
    async def handle_bind_cn(self, username: str) -> tuple[bool, str]:
        return await self.handle_bind(username)

    @cmd_route("unbind")
    async def handle_unbind(self) -> tuple[bool, str]:
        message = self._message
        if not _is_private(message):
            return True, "解绑请私聊使用 /api unbind。"
        person_id = _person_id(message)
        if not person_id:
            return True, "无法识别你的 QQ 身份。"
        removed = await self._service().unbind_account(person_id)
        return True, "已解除绑定。" if removed else "当前 QQ 没有绑定账号。"

    @cmd_route("解绑")
    async def handle_unbind_cn(self) -> tuple[bool, str]:
        return await self.handle_unbind()

    @cmd_route("trend")
    async def handle_trend(self, days: int = 7) -> tuple[bool, str]:
        message = self._message
        if not _is_private(message):
            return True, "调用趋势包含个人用量信息，请私聊使用 /api trend [7|30]。"
        if days not in (7, 30):
            return True, "用法：/api trend [7|30]，例如 /api trend 7。"
        person_id = _person_id(message)
        if not person_id:
            return True, "无法识别你的 QQ 身份。"
        try:
            username, rows, metrics = await self._service().get_trend(person_id, days)
        except Exception as exc:
            return True, f"查询失败：{exc}"

        total_requests = sum(row["requests"] for row in rows)
        total_quota = sum(row["quota"] for row in rows)
        lines = [
            f"NewAPI 调用趋势：{username}，最近 {days} 天",
            f"总调用 {total_requests:,} 次；总额度 {_format_quota(total_quota)}",
        ]
        if metrics is None:
            lines.append("延迟、Token 和模型统计暂时无法从 NewAPI 日志读取。")
        else:
            sampled = metrics["records_sampled"]
            total = metrics["records_total"]
            if metrics["partial"]:
                lines.append(f"附加指标覆盖 {sampled:,}/{total:,} 条消费日志。")
            if metrics["latency_count"]:
                lines.append(
                    "调用延迟（"
                    f"{metrics['latency_count']:,} 条有效记录）："
                    f"平均 {metrics['avg_latency']:.1f}s，"
                    f"P95 {metrics['p95_latency']:.1f}s，"
                    f"峰值 {metrics['max_latency']:.1f}s"
                )
            else:
                lines.append("调用延迟：日志中暂无有效耗时记录。")
            token_label = "消费日志 Token（样本）" if metrics["partial"] else "消费日志 Token"
            lines.append(
                f"{token_label}：输入 {metrics['prompt_tokens']:,}，"
                f"输出 {metrics['completion_tokens']:,}"
            )
            if metrics["models"]:
                lines.append("各模型用量与延迟（按调用数排序，覆盖本次读取的全部模型）：")
                for model in metrics["models"]:
                    if model["latency_count"]:
                        latency = (
                            f"延迟 {model['latency_count']:,} 条："
                            f"均值 {model['avg_latency']:.1f}s / "
                            f"P95 {model['p95_latency']:.1f}s / "
                            f"峰值 {model['max_latency']:.1f}s"
                        )
                    else:
                        latency = "无有效耗时"
                    lines.append(
                        f"{model['model']}：{model['calls']:,} 次，{latency}；"
                        f"Token 输入 {model['prompt_tokens']:,} / 输出 {model['completion_tokens']:,}"
                    )
            else:
                lines.append("所选时间范围内没有消费日志模型记录。")
        lines.extend(
            f"{row['date']:%m-%d}  {row['requests']:,} 次  额度 {_format_quota(row['quota'])}"
            for row in rows
        )
        return await self._reply_trend(lines)

    @cmd_route("趋势")
    async def handle_trend_cn(self, days: int = 7) -> tuple[bool, str]:
        return await self.handle_trend(days)

    @cmd_route("调用趋势")
    async def handle_usage_trend_cn(self, days: int = 7) -> tuple[bool, str]:
        return await self.handle_trend(days)


class GroupGateHandler(BaseEventHandler):
    name = "group_gate"
    description = "受管群仅允许已绑定 NewAPI 账号的用户加入"
    weight = 100
    init_subscribe = [EventType.ON_MESSAGE_RECEIVED]

    async def execute(
        self, event_name: str, params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        message = params.get("message")
        if message is None:
            return EventDecision.PASS, params

        request = _request_meta(message)
        if request is not None:
            return await self._handle_group_request(request, params)

        notice = _group_increase_meta(message)
        if notice is not None:
            return await self._handle_group_increase(notice, params)

        return EventDecision.PASS, params

    async def _handle_group_request(
        self, request: dict[str, Any], params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        service = NewAPIService(self.plugin)
        group_id = str(request.get("group_id") or "").strip()
        user_id = str(request.get("user_id") or "").strip()
        flag = str(request.get("flag") or "").strip()
        sub_type = str(request.get("sub_type") or "add").strip()

        if (
            not service.gate_enabled()
            or group_id not in service.managed_group_ids()
            or sub_type != "add"
        ):
            return EventDecision.PASS, params
        if not user_id or not flag:
            return EventDecision.STOP, params

        person_id = generate_person_id("qq", user_id)
        binding = await service.get_binding(person_id)
        approve = binding is not None
        reason = "" if approve else "请先私聊机器人绑定 NewAPI 账号，再重新申请入群。"
        handled = await self._submit_request_decision(flag, sub_type, approve, reason)
        return (EventDecision.STOP if handled else EventDecision.PASS), params

    async def _handle_group_increase(
        self, notice: dict[str, Any], params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        service = NewAPIService(self.plugin)
        group_id = str(notice.get("group_id") or "").strip()
        user_id = str(notice.get("user_id") or "").strip()
        if (
            not service.post_join_kick_enabled()
            or group_id not in service.managed_group_ids()
            or not group_id
            or not user_id
        ):
            return EventDecision.PASS, params

        person_id = generate_person_id("qq", user_id)
        if await service.get_binding(person_id) is not None:
            return EventDecision.PASS, params

        adapter_sign = self._pick_adapter()
        if not adapter_sign:
            return EventDecision.PASS, params
        result = await adapter_api.send_adapter_command(
            adapter_sign=adapter_sign,
            command_name="set_group_kick",
            command_data={
                "group_id": int(group_id),
                "user_id": int(user_id),
                "reject_add_request": False,
            },
            timeout=10.0,
        )
        handled = isinstance(result, dict) and result.get("status") == "ok"
        return (EventDecision.STOP if handled else EventDecision.PASS), params

    async def _submit_request_decision(
        self, flag: str, sub_type: str, approve: bool, reason: str
    ) -> bool:
        adapter_sign = self._pick_adapter()
        if not adapter_sign:
            return False
        result = await adapter_api.send_adapter_command(
            adapter_sign=adapter_sign,
            command_name="set_group_add_request",
            command_data={
                "flag": flag,
                "sub_type": sub_type,
                "approve": approve,
                "reason": reason,
            },
            timeout=10.0,
        )
        return isinstance(result, dict) and result.get("status") == "ok"

    @staticmethod
    def _pick_adapter() -> str | None:
        return _pick_onebot_adapter()


class ChartCommandHandler(BaseEventHandler):
    name = "chart_events"
    description = "处理受管 QQ 群的 /nai 延迟图命令"
    weight = 50
    init_subscribe = [EventType.ON_MESSAGE_RECEIVED]

    async def execute(
        self, event_name: str, params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        config = getattr(self.plugin.config, "chart", None)
        if not config or not bool(getattr(config, "enabled", False)):
            return EventDecision.PASS, params

        message = params.get("message")
        if message is None or not _is_group_message(message) or _is_self_message(message):
            return EventDecision.PASS, params
        if _message_group_id(message) != str(getattr(config, "group_id", "") or "").strip():
            return EventDecision.PASS, params

        parts = _message_text(message).strip().split()
        if not parts or parts[0].lower() != "/nai" or len(parts) > 2:
            return EventDecision.PASS, params

        try:
            maximum = min(max(int(getattr(config, "max_minutes", 10080)), 1), 10080)
            minutes = int(parts[1]) if len(parts) == 2 else int(getattr(config, "default_minutes", 1440))
        except (TypeError, ValueError):
            return EventDecision.PASS, params
        if not 1 <= minutes <= maximum:
            return EventDecision.PASS, params

        await self.plugin.enqueue_chart(minutes, _message_id(message))
        return EventDecision.STOP, params


@register_plugin
class NewAPIGroupManagerPlugin(BasePlugin):
    plugin_name = PLUGIN_NAME
    plugin_description = "绑定 NewAPI 账号、查询模型延迟，提供 LLM 查询工具，管理 QQ 群并推送 NAI 延迟图"
    plugin_version = "0.4.0"
    configs: list[type] = [NewAPIGroupManagerConfig]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._state_lock = asyncio.Lock()
        self._chart_queue: asyncio.Queue[int] = asyncio.Queue(maxsize=2)
        self._chart_tasks: list[asyncio.Task[None]] = []
        self._chart_seen: dict[str, float] = {}
        self._chart_seen_lock = asyncio.Lock()

    def get_components(self) -> list[type]:
        config = self.config
        if isinstance(config, NewAPIGroupManagerConfig) and not config.plugin.enabled:
            return []
        return [
            NewAPICommand,
            NewAPIAccountStatusTool,
            NewAPIUsageTrendTool,
            GroupGateHandler,
            ChartCommandHandler,
        ]

    async def on_plugin_loaded(self) -> None:
        config = getattr(self.config, "chart", None)
        if not config or not bool(getattr(config, "enabled", False)):
            return
        if not str(getattr(config, "gateway_url", "") or "").strip() or not str(
            getattr(config, "group_id", "") or ""
        ).strip():
            logger.warning("NAI chart feature is enabled but gateway_url or group_id is missing")
            return

        self._chart_tasks = [
            asyncio.create_task(self._chart_worker(), name="newapi-chart-worker"),
            asyncio.create_task(self._hourly_chart_loop(), name="newapi-chart-scheduler"),
        ]

    async def on_plugin_unloaded(self) -> None:
        for task in self._chart_tasks:
            task.cancel()
        if self._chart_tasks:
            await asyncio.gather(*self._chart_tasks, return_exceptions=True)
        self._chart_tasks.clear()

    async def enqueue_chart(self, minutes: int, message_id: str = "") -> bool:
        async with self._chart_seen_lock:
            now = time.monotonic()
            for key, timestamp in list(self._chart_seen.items()):
                if now - timestamp > 300:
                    del self._chart_seen[key]
            if message_id and message_id in self._chart_seen:
                return True
            try:
                self._chart_queue.put_nowait(minutes)
            except asyncio.QueueFull:
                logger.warning("NAI chart skipped because the delivery queue is full")
                return False
            if message_id:
                self._chart_seen[message_id] = now
            return True

    async def _chart_worker(self) -> None:
        while True:
            minutes = await self._chart_queue.get()
            try:
                await GatewayChartService(self).send_chart(minutes)
                logger.info("NAI chart sent to configured group for %s minutes", minutes)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("NAI chart delivery failed")
            finally:
                self._chart_queue.task_done()

    async def _hourly_chart_loop(self) -> None:
        config = self.config.chart
        interval = min(max(int(getattr(config, "interval_seconds", 3600)), 60), 86400)
        minutes = int(getattr(config, "default_minutes", 1440))
        while True:
            await asyncio.sleep(interval)
            await self.enqueue_chart(minutes)
