# MoFox NewAPI 群管理插件

将 QQ 用户绑定到 NewAPI 账号，供用户查询自己的调用趋势，并按绑定状态审核指定 QQ 群的加群申请。

## 功能

- `/api bind <NewAPI 用户名>`：私聊绑定 NewAPI 用户名，不需要提供密钥。
- `/api help`：查看命令用法；未知的 `/api` 子命令会提示查看帮助。
- `/api unbind`：私聊解除 QQ 账号绑定。
- `/api trend [7|30]`：私聊查询最近 7 天或 30 天的每日消费调用数、额度、Token 用量，以及日志中每个模型各自的平均、P95 和峰值延迟；回复较长时合并转发。
- LLM 工具 `newapi_account_status`：检查当前私聊用户的绑定状态。
- LLM 工具 `newapi_usage_trend`：按当前私聊用户身份查询 7 天或 30 天的调用趋势、Token 用量和所有模型的延迟统计。
- 对配置的受管群，已绑定用户的加群申请会获准，未绑定用户会被拒绝。
- 若适配器没有转发加群申请事件，可选在未绑定用户入群后将其移出。
- 在图表目标群发送 `/nai` 或 `/nai 60`，获取最近一天或指定分钟数的模型延迟图；默认每小时自动推送一次。

绑定数据通过 MoFox `storage_api` 保存在插件自己的 JSON 命名空间中。

两个 LLM 工具只在私聊中工作，并从触发消息读取当前 QQ 身份；不会接受模型提供的其他用户 ID。未绑定时提示用户直接使用 `/api bind <用户名>`，工具不会读取或处理密钥。用户名绑定按用户自报值关联，不会验证该用户是否拥有对应 NewAPI 账号；请只在成员可信的群中启用绑定门禁。要让模型自动调用工具，当前聊天器和所用模型需启用并支持 Tool Calling。

## 安装与配置

1. 将 `newapi_group_manager` 插件目录放入 MoFox 的 `plugins/` 目录。
2. 在运行 MoFox 的容器环境中设置 `NEWAPI_ADMIN_TOKEN`。该令牌应属于有权限读取日志和日志统计的 NewAPI Admin/Root 账号，并尽量使用单独的只读面板 PAT。不要使用推理 API Key 或站点账号密码。
3. 配置 `config/plugins/newapi_group_manager/config.toml`：

   ```toml
   [newapi]
   base_url = "https://your-newapi-host"
   admin_token_env = "NEWAPI_ADMIN_TOKEN"
   allow_insecure_http = false
   timeout_seconds = 10
   max_latency_logs = 5000
   timezone = "Asia/Shanghai"

   [group]
   gate_enabled = false
   managed_group_ids = []
   kick_unbound_after_join = false

   [chart]
   enabled = false
   gateway_url = "https://your-nai-gateway-host"
   gateway_password_env = "NAI_GATEWAY_PASSWORD"
   allow_insecure_http = false
   group_id = "123456789"
   interval_seconds = 3600
   default_minutes = 1440
   max_minutes = 10080
   timeout_seconds = 60
   ```

4. 把需要绑定才能加入的 QQ 群号填入 `managed_group_ids`，并设置 `gate_enabled = true`。

插件默认拒绝通过 HTTP 发送 Bearer 令牌。你提供的 NewAPI 和 NAI 网关地址都使用 HTTP；启用绑定、趋势和延迟图前，建议先为两个站点配置 HTTPS。只有在可信内网环境中，才考虑在对应配置节显式设置 `allow_insecure_http = true`，此时令牌或网关密码不会被传输加密保护。

请将 `NAI_GATEWAY_PASSWORD` 设置在 MoFox 容器环境中，不要把密码填进配置文件。`/nai` 的分钟数限制为 1 到 `max_minutes`（最高 10080 分钟）；自动推送首次在插件加载后的一个完整间隔结束时执行。

Docker Compose 可通过宿主机 `.env` 注入这两个凭据，`.env` 不要提交到版本库：

```yaml
services:
  mofox:
    environment:
      NEWAPI_ADMIN_TOKEN: ${NEWAPI_ADMIN_TOKEN}
      NAI_GATEWAY_PASSWORD: ${NAI_GATEWAY_PASSWORD}
```

## 绑定流程

用户在 QQ 私聊中发送 `/api bind <用户名>` 即可绑定；插件只保存用户自报的 NewAPI 用户名，不请求用户 PAT，也不验证用户名归属。绑定后趋势查询以该用户名筛选消费统计。

趋势查询使用容器环境变量中的管理员 PAT。插件不会把管理员 PAT 返回给用户或主动写入日志。请根据正在使用的 NewAPI 版本为 PAT 授予读取日志和统计所需的最低权限。

`/api trend` 的延迟来自 NewAPI 消费日志中的 `use_time` 字段，按 `model_name` 分组；每个有消费记录的模型都会列出调用数、有效延迟样本数、平均值、P95、峰值和 Token 用量。`max_latency_logs` 限制附加统计最多读取的日志条数（客户端上限 20000）；超出时会提示结果只覆盖已读取的日志样本。每日总调用数和额度仍通过趋势统计接口读取。

## 加群申请支持

处理器会查找 MoFox 消息信封中的 `message_info.extra.is_request`、`request_type`、`sub_type`、`group_id`、`user_id` 和 `flag` 字段，再调用 OneBot `set_group_add_request` 动作。

当前内置 `onebot_adapter` 文档说明了通知事件及其消息信封，但没有记录加群申请事件的转发。因此，入群前审核要求所用适配器把 SnowLuma 的 `request` 事件转成 MoFox 消息信封。如果适配器只提供 `group_increase` 入群通知，可对指定群启用 `kick_unbound_after_join = true`，在入群后移出未绑定用户。此兜底默认关闭；启用前应先确认适配器确实会转发入群通知。

## 调用的 NewAPI 接口

- 用户绑定：不调用用户凭据接口，只保存用户自报的 NewAPI 用户名。
- 每日趋势：`GET /api/log/stat` 和 `GET /api/log/`，按用户名、消费类型 `2` 及本地时区当天的起止时间筛选。
- 延迟与模型分布：分页读取所选时间范围的 `GET /api/log/` 消费日志，根据 `model_name` 和 `use_time` 汇总所有在日志中出现的模型。

NewAPI 不同版本或分支的日志接口权限和响应可能不同。插件按标准 `{success, data}` 响应处理，并要求分页列表响应包含 `data.total`。

## 参考文档

- [MoFox 插件开发](https://docs.mofox.chat/docs/development/plugin_develop/guide/plugin-authoring/3-first-plugin)
- [MoFox 插件配置](https://docs.mofox.chat/docs/development/plugin_develop/guide/plugin-authoring/6-config)
- [MoFox 事件系统](https://docs.mofox.chat/docs/development/plugin_develop/guide/plugin-authoring/15-event-system)
- [MoFox 内置事件参考](https://docs.mofox.chat/docs/development/plugin_develop/guide/plugin-authoring/15.5-built-in-events)
- [MoFox 存储框架](https://docs.mofox.chat/docs/development/plugin_develop/guide/plugin-authoring/22-storage-framework)
- [MoFox 适配器命令](https://docs.mofox.chat/docs/development/plugin_develop/guide/plugin-authoring/21-adapter-commands)
- [MoFox OneBot 适配器概览](https://docs.mofox.chat/docs/builtin_plugins/onebot/dev-guide/overview)
- [NewAPI 日志接口](https://docs.newapi.ai/zh/docs/guide/feature-guide/user/log)
- [NewAPI Token 认证说明](https://github.com/QuantumNous/new-api/blob/main/docs/authentication.md)
