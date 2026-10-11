from __future__ import annotations

import asyncio
import base64
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from src.app.plugin_system.api import adapter_api

from .client import NewAPIError


MAX_IMAGE_BYTES = 12 * 1024 * 1024


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        # Do not forward the gateway password to an upstream-selected URL.
        return None


class GatewayChartService:
    """Fetches a latency PNG from the gateway and sends it through OneBot."""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.config = plugin.config.chart

    def _request_settings(self, minutes: int) -> tuple[str, str, int]:
        base_url = str(getattr(self.config, "gateway_url", "") or "").strip()
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise NewAPIError("NAI 网关地址未配置或格式无效。")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise NewAPIError("NAI 网关地址不能包含账号、密码、查询参数或片段。")
        allow_http = bool(getattr(self.config, "allow_insecure_http", False))
        if parsed.scheme == "http" and not allow_http:
            raise NewAPIError("NAI 网关使用未加密 HTTP；请配置 HTTPS，或在可信内网中显式允许 HTTP。")

        env_name = str(
            getattr(self.config, "gateway_password_env", "NAI_GATEWAY_PASSWORD")
            or "NAI_GATEWAY_PASSWORD"
        ).strip()
        password = os.getenv(env_name, "").strip() if env_name else ""
        if not password:
            raise NewAPIError("NAI 网关密码尚未配置到容器环境变量。")

        maximum = min(max(int(getattr(self.config, "max_minutes", 10080)), 1), 10080)
        safe_minutes = min(max(int(minutes), 1), maximum)
        path = parsed.path.rstrip("/") + "/admin/api/model-latency.png"
        url = urlunsplit((parsed.scheme, parsed.netloc, path, urlencode({"minutes": safe_minutes}), ""))
        timeout = min(max(int(getattr(self.config, "timeout_seconds", 60)), 2), 120)
        return url, password, timeout

    def _fetch_png_sync(self, minutes: int) -> bytes:
        url, password, timeout = self._request_settings(minutes)
        request = Request(
            url,
            headers={
                "Accept": "image/png",
                "Authorization": f"Bearer {password}",
                "User-Agent": "MoFox-NewAPI-GroupManager/0.4.0",
            },
            method="GET",
        )
        opener = build_opener(_NoRedirectHandler())
        try:
            with opener.open(request, timeout=timeout) as response:
                image = response.read(MAX_IMAGE_BYTES + 1)
        except HTTPError as exc:
            if 300 <= exc.code < 400:
                raise NewAPIError("NAI 网关返回了跳转，请把 gateway_url 改为最终地址。") from None
            if exc.code in (401, 403):
                raise NewAPIError("NAI 网关拒绝了密码，请检查网关密码配置。") from None
            raise NewAPIError(f"NAI 网关取图失败（HTTP {exc.code}）。") from None
        except (URLError, TimeoutError, OSError):
            raise NewAPIError("暂时无法连接 NAI 网关，请检查地址和网络。") from None

        if len(image) > MAX_IMAGE_BYTES:
            raise NewAPIError("NAI 网关返回的图片过大。")
        if not image.startswith(b"\x89PNG\r\n\x1a\n"):
            raise NewAPIError("NAI 网关没有返回有效的 PNG 图片。")
        return image

    async def send_chart(self, minutes: int) -> None:
        group_id = str(getattr(self.config, "group_id", "") or "").strip()
        if not group_id.isdigit():
            raise NewAPIError("图表接收群号未配置或格式无效。")
        image = await asyncio.to_thread(self._fetch_png_sync, minutes)
        adapter_sign = self._pick_adapter()
        if not adapter_sign:
            raise NewAPIError("没有正在运行的 OneBot / SnowLuma 适配器。")

        result = await adapter_api.send_adapter_command(
            adapter_sign=adapter_sign,
            command_name="send_group_msg",
            command_data={
                "group_id": int(group_id),
                "message": [
                    {
                        "type": "image",
                        "data": {"file": "base64://" + base64.b64encode(image).decode("ascii")},
                    }
                ],
            },
            timeout=min(max(int(getattr(self.config, "timeout_seconds", 60)), 2), 120),
        )
        if not isinstance(result, dict) or result.get("status") != "ok":
            raise NewAPIError("OneBot 适配器未能发送延迟图。")

    @staticmethod
    def _pick_adapter() -> str | None:
        active = adapter_api.list_active_adapters()
        return next(
            (
                signature
                for signature in active
                if "onebot" in str(signature).lower() or "snowluma" in str(signature).lower()
            ),
            None,
        )
