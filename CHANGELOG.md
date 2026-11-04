# Changelog

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.1.0] — 2026-11-04

首个发布版本：把「把网上的东西读清楚」这一件事做完整。

### 能力

- `search_web(query, count=10, need_content=False, ori_question="")`：
  DeepSeek `/v1/tools/search`，返回 600~800 字摘要（定位用）。
- `fetch_web(url, max_chars=8000)`：`/v1/tools/fetch`，抓正文（读全文用）。
- ReAct 循环、工具调度、上下文压缩、多轮记忆全部交给 AgentScope，本项目一行没重写。

### CLI

- `openagent-ds-search "问题"`：单次问答。
- `openagent-ds-search -i`：多轮交互；`/help` `/context` `/compact` `/reset` `/exit`。
- `openagent-ds-search --check`：模型自证 + 上下文窗口自证 + search/fetch 各真打一次。
- `openagent-ds-search --dump`：不进模型，直看工具原始返回。
- 别名 `ds-search` 与 `python -m openagent_ds_search` 等价。

### 安全

- 只读环境变量，不自动加载 `.env`；仓库内不含任何真实密钥。
- 网页内容按不可信数据处理（提示注入防线）；内网地址七种一律拒绝（SSRF 防线）。
- 每次运行的 search / fetch 次数有硬上限，超了不发请求、不计费。

### 质量

- 128 个离线测试全绿（`pytest -q`），真实网络验收通过（2026-11-04）。

[0.1.0]: https://github.com/YY-Yifei/openagent-ds-search/releases/tag/v0.1.0
