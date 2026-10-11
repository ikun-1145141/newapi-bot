from __future__ import annotations

import asyncio
import json
import math
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NewAPIError(RuntimeError):
    """An API or configuration error safe to show to a bot user."""


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        # Never forward a bearer token to a URL selected by an upstream redirect.
        return None


class NewAPIClient:
    """Small stdlib-only NewAPI client using dashboard Bearer tokens."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_seconds: int = 10,
        allow_insecure_http: bool = False,
    ) -> None:
        parsed = urlsplit((base_url or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise NewAPIError("NewAPI 地址未配置或格式无效。")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise NewAPIError("NewAPI 地址不能包含账号、密码、查询参数或片段。")
        if parsed.scheme == "http" and not allow_insecure_http:
            raise NewAPIError(
                "NewAPI 地址使用未加密 HTTP；请配置 HTTPS，或仅在可信内网中显式允许 HTTP。"
            )
        if not token.strip():
            raise NewAPIError("NewAPI 令牌未配置。")

        self.base_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
        self.token = token.strip()
        self.timeout_seconds = min(max(int(timeout_seconds), 2), 60)
        self._opener = build_opener(_NoRedirectHandler())

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_json_sync, path, params)

    def _get_json_sync(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = urlencode(params or {})
        url = f"{self.base_url}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{query}"

        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.token}",
                "User-Agent": "MoFox-NewAPI-GroupManager/0.4.0",
            },
            method="GET",
        )
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                raw_body = response.read(1_000_000)
        except HTTPError as exc:
            if 300 <= exc.code < 400:
                raise NewAPIError("NewAPI 返回了跳转，请把 base_url 改为最终地址。") from None
            if exc.code in (401, 403):
                raise NewAPIError("NewAPI 拒绝了令牌；请检查令牌有效期、账号角色和读取权限。") from None
            raise NewAPIError(f"NewAPI 请求失败（HTTP {exc.code}）。") from None
        except (URLError, TimeoutError, OSError):
            raise NewAPIError("暂时无法连接 NewAPI，请检查地址和网络。") from None

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise NewAPIError("NewAPI 返回了无法识别的响应。") from None
        if not isinstance(payload, dict):
            raise NewAPIError("NewAPI 返回了无法识别的响应。")
        if payload.get("success") is False:
            message = str(payload.get("message") or "接口返回失败")[:160]
            raise NewAPIError(f"NewAPI：{message}")
        return payload

    @staticmethod
    def _data(payload: dict[str, Any]) -> Any:
        return payload.get("data", payload)

    async def get_self(self) -> dict[str, Any]:
        payload = await self._get_json("/api/user/self")
        data = self._data(payload)
        if not isinstance(data, dict):
            raise NewAPIError("这个令牌无法读取 NewAPI 个人资料；请使用面板个人访问令牌。")
        username = str(data.get("username") or "").strip()
        user_id = data.get("id", data.get("user_id"))
        if not username or user_id is None:
            raise NewAPIError("这个令牌没有返回有效的 NewAPI 用户资料。")
        return {"user_id": str(user_id), "username": username}

    async def get_daily_usage(self, username: str, start: int, end: int) -> dict[str, int]:
        common = {
            "type": 2,
            "start_timestamp": start,
            "end_timestamp": end,
            "username": username,
        }
        stat_payload, logs_payload = await asyncio.gather(
            self._get_json("/api/log/stat", common),
            self._get_json("/api/log/", {**common, "p": 1, "page_size": 1}),
        )

        stat = self._data(stat_payload)
        page = self._data(logs_payload)
        if not isinstance(stat, dict):
            raise NewAPIError("NewAPI 的日志统计响应格式不正确。")
        if not isinstance(page, dict):
            raise NewAPIError("NewAPI 的日志列表响应格式不正确。")
        total = page.get("total", page.get("total_count", 0))
        try:
            return {
                "quota": max(0, int(stat.get("quota", 0) or 0)),
                "requests": max(0, int(total or 0)),
            }
        except (TypeError, ValueError):
            raise NewAPIError("NewAPI 返回了无法识别的趋势数据。") from None

    async def get_usage_metrics(
        self, username: str, start: int, end: int, *, max_records: int = 5000
    ) -> dict[str, Any]:
        """Read a bounded set of consumption logs for latency/token summaries."""
        max_records = min(max(int(max_records), 1), 20000)
        page_size = 100
        records: list[dict[str, Any]] = []
        reported_total: int | None = None
        page_number = 1

        while len(records) < max_records:
            size = min(page_size, max_records - len(records))
            payload = await self._get_json(
                "/api/log/",
                {
                    "type": 2,
                    "start_timestamp": start,
                    "end_timestamp": end,
                    "username": username,
                    "p": page_number,
                    "page_size": size,
                },
            )
            data = self._data(payload)
            if not isinstance(data, dict):
                raise NewAPIError("NewAPI 的日志明细响应格式不正确。")
            items = data.get("items", data.get("logs", []))
            if not isinstance(items, list):
                raise NewAPIError("NewAPI 的日志明细响应格式不正确。")
            try:
                if data.get("total") is not None:
                    reported_total = max(0, int(data["total"]))
            except (TypeError, ValueError):
                reported_total = None

            if not items:
                break
            previous_count = len(records)
            records.extend(item for item in items if isinstance(item, dict))
            if len(records) == previous_count:
                break
            if reported_total is not None and len(records) >= reported_total:
                break
            if reported_total is None and len(items) < size:
                break
            page_number += 1

        latencies: list[float] = []
        models: dict[str, dict[str, Any]] = {}
        prompt_tokens = 0
        completion_tokens = 0
        for record in records:
            model_name = str(record.get("model_name") or "未知模型").strip() or "未知模型"
            model_stats = models.setdefault(
                model_name,
                {"calls": 0, "latencies": [], "prompt_tokens": 0, "completion_tokens": 0},
            )
            model_stats["calls"] += 1
            try:
                prompt_count = max(0, int(record.get("prompt_tokens", 0) or 0))
            except (TypeError, ValueError):
                prompt_count = 0
            try:
                completion_count = max(0, int(record.get("completion_tokens", 0) or 0))
            except (TypeError, ValueError):
                completion_count = 0
            prompt_tokens += prompt_count
            completion_tokens += completion_count
            model_stats["prompt_tokens"] += prompt_count
            model_stats["completion_tokens"] += completion_count
            try:
                use_time = float(record.get("use_time", 0) or 0)
            except (TypeError, ValueError):
                use_time = 0
            if math.isfinite(use_time) and use_time > 0:
                latencies.append(use_time)
                model_stats["latencies"].append(use_time)

        latencies.sort()
        if latencies:
            p95_index = max(0, math.ceil(len(latencies) * 0.95) - 1)
            avg_latency = sum(latencies) / len(latencies)
            p95_latency = latencies[p95_index]
            max_latency = latencies[-1]
        else:
            avg_latency = p95_latency = max_latency = None

        model_rows = []
        for model_name, values in models.items():
            model_latencies = sorted(values["latencies"])
            if model_latencies:
                model_p95_index = max(0, math.ceil(len(model_latencies) * 0.95) - 1)
                model_avg_latency = sum(model_latencies) / len(model_latencies)
                model_p95_latency = model_latencies[model_p95_index]
                model_max_latency = model_latencies[-1]
            else:
                model_avg_latency = model_p95_latency = model_max_latency = None
            model_rows.append(
                {
                    "model": model_name,
                    "calls": values["calls"],
                    "avg_latency": model_avg_latency,
                    "p95_latency": model_p95_latency,
                    "max_latency": model_max_latency,
                    "latency_count": len(model_latencies),
                    "prompt_tokens": values["prompt_tokens"],
                    "completion_tokens": values["completion_tokens"],
                }
            )
        model_rows.sort(key=lambda item: item["calls"], reverse=True)

        total = reported_total if reported_total is not None else len(records)
        return {
            "records_total": total,
            "records_sampled": len(records),
            "partial": total > len(records),
            "latency_count": len(latencies),
            "avg_latency": avg_latency,
            "p95_latency": p95_latency,
            "max_latency": max_latency,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "models": model_rows,
        }
