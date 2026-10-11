from __future__ import annotations

from typing import ClassVar

from src.app.plugin_system.base import BaseConfig, Field, SectionBase, config_section


class NewAPIGroupManagerConfig(BaseConfig):
    """Settings for the NewAPI group manager plugin."""

    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = "NewAPI 账号绑定与群管理配置"

    @config_section("plugin")
    class PluginSection(SectionBase):
        enabled: bool = Field(default=True, description="是否启用插件")

    @config_section("newapi")
    class NewAPISection(SectionBase):
        base_url: str = Field(
            default="",
            description="NewAPI 面板根地址，例如 https://api.example.com；不要带 /api 路径",
        )
        admin_token_env: str = Field(
            default="NEWAPI_ADMIN_TOKEN",
            description="读取管理员只读 PAT 的环境变量名，不要把令牌写入配置文件",
        )
        allow_insecure_http: bool = Field(
            default=False,
            description="允许通过未加密 HTTP 发送令牌；仅应在可信内网中启用",
        )
        timeout_seconds: int = Field(
            default=10,
            description="NewAPI HTTP 请求超时秒数",
        )
        max_latency_logs: int = Field(
            default=5000,
            description="趋势额外指标最多读取的消费日志条数，范围上限 20000",
        )
        timezone: str = Field(
            default="Asia/Shanghai",
            description="趋势日期使用的时区",
        )

    @config_section("group")
    class GroupSection(SectionBase):
        gate_enabled: bool = Field(
            default=False,
            description="启用受管群加群申请审核；仅对下面列出的群生效",
        )
        managed_group_ids: list[str] = Field(
            default_factory=list,
            description="由本插件管理的 QQ 群号列表",
        )
        kick_unbound_after_join: bool = Field(
            default=False,
            description="适配器未转发加群申请时，可在检测到入群通知后踢出未绑定用户",
        )

    @config_section("chart")
    class ChartSection(SectionBase):
        enabled: bool = Field(
            default=False,
            description="启用群内 /nai 延迟图命令和每小时自动推送",
        )
        gateway_url: str = Field(
            default="",
            description="NAI 网关根地址，例如 https://gateway.example.com",
        )
        gateway_password_env: str = Field(
            default="NAI_GATEWAY_PASSWORD",
            description="读取 NAI 网关密码的环境变量名，不要把密码写入配置文件",
        )
        allow_insecure_http: bool = Field(
            default=False,
            description="允许通过未加密 HTTP 发送网关密码；仅在可信内网中启用",
        )
        group_id: str = Field(
            default="",
            description="接收图表命令与每小时推送的 QQ 群号",
        )
        interval_seconds: int = Field(
            default=3600,
            description="自动推送间隔秒数",
        )
        default_minutes: int = Field(
            default=1440,
            description="/nai 未指定分钟数时的默认范围，也用于自动推送",
        )
        max_minutes: int = Field(
            default=10080,
            description="图表允许查询的最大分钟数",
        )
        timeout_seconds: int = Field(
            default=60,
            description="网关取图和 OneBot 发图超时秒数",
        )

    plugin: PluginSection = Field(default_factory=PluginSection)
    newapi: NewAPISection = Field(default_factory=NewAPISection)
    group: GroupSection = Field(default_factory=GroupSection)
    chart: ChartSection = Field(default_factory=ChartSection)
