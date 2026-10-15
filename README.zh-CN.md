# DraftGate

> AI 内容管线上的人工检查站。

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](pyproject.toml)
[![English](https://img.shields.io/badge/docs-English-blue)](README.md)

AI 管线负责起草内容，**能不能发出去由你决定**。

DraftGate 是一个轻量的自托管审核站，适配任何 AI 工作流——n8n、Dify、自研脚本、
定时任务。内容进入审核队列后，人在浏览器里点击**通过 / 拒绝 / 修改后通过**，
决定通过签名 webhook 或阻塞式 SDK 调用回传给管线。

```
 你的管线 ──提交──►  DraftGate  ──webhook──► 你的管线
 (n8n/Dify/脚本)     人工审核               发布或丢弃
```

## 解决什么问题

AI 生成的内容发出去之前总需要人过一眼——周报、社媒帖子、摘要邮件。大多数团队
用一个表格、一个群聊，或者干脆裸奔来解决。DraftGate 把它变成 5 分钟的集成：

- **零配置服务** — SQLite 存储、token 认证、pip 一装即用
- **三种决策，都带反馈** — 通过 / 拒绝 / 修改后通过；修改稿和审核备注（拒绝时通常要写）一起回传管线，让它知道该怎么改，而不是瞎猜
- **签名 webhook** — HMAC-SHA256 签名、自动退避重试、UI 手动重发
- **阻塞式 SDK** — `gate.review(...)` 让脚本停在原地等人拍板
- **CLI 退出码** — `0=通过 1=拒绝 2=修改后通过`，n8n / shell 直接分支
- **单文件网页 UI** — 无 Node 构建、无前端框架，安静轮询

## 快速开始

```bash
pip install git+https://github.com/buxiangcifan123/draftgate.git

draftgate serve                 # 启动在 http://127.0.0.1:8010
draftgate token                 # 复制这个 token
```

打开 [http://127.0.0.1:8010](http://127.0.0.1:8010)，粘贴 token，审核队列就有了。

提交第一份草稿：

```bash
export DRAFTGATE_TOKEN=$(draftgate token)
echo "你好，请审核这份草稿。" > draft.txt

draftgate submit --title "我的第一份草稿" --file draft.txt --timeout 600
echo "exit code: $?"    # 0=通过  1=拒绝  2=修改后通过
```

命令会阻塞到有人在浏览器里点完按钮，然后以管线可以直接分支的退出码结束。
最终结果（含人工修改稿）以 JSON 打印到 stdout。

## SDK

```python
from draftgate import Client

gate = Client("http://127.0.0.1:8010", token="...")

result = gate.review(
    title="AI 每周新闻摘要",
    content=markdown_text,
    content_type="markdown",
    tags=["周报"],
    timeout=600,          # 0 = 非阻塞
)

if result["status"] == "edited":
    publish(result["final_content"])   # 人工修改后的内容
elif result["status"] == "approved":
    publish(result["content"])
```

## Webhook

提交时带上 `callback_url`，DraftGate 会把决策 POST 回去：

```json
{
  "item_id": 7,
  "action": "approve",
  "status": "approved",
  "title": "每周摘要",
  "content": "最终内容（action=edit 时为修改稿）",
  "reason": "拒绝原因，没有则为 null",
  "tags": ["周报"],
  "metadata": {"任意": "自定义字段"},
  "decided_at": "2026-10-15 09:30:00"
}
```

每个请求都带签名，接收方这样验证：

```python
import hashlib, hmac

def is_valid(secret: str, raw_body: bytes, header: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header, expected)
```

投递失败会自动重试（10 秒 / 1 分 / 5 分），也可以在 UI 里手动重发。
**请把 webhook 当作"至少送达一次"，按 `item_id` 做幂等处理。**
本地调试可运行 `examples/mock_receiver.py` 观察投递。

## REST API

服务启动后访问 `/docs` 有交互式文档。速览：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/items` | 提交草稿 |
| GET | `/api/items?status=&limit=&offset=` | 列表（pending 置顶） |
| GET | `/api/items/{id}` | 详情（含 `final_content`） |
| POST | `/api/items/{id}/decision` | `{"action": "approve"\|"reject"\|"edit", "content": "...", "reason": "..."}` |
| POST | `/api/items/{id}/webhook/retry` | 重发失败的 webhook |
| GET | `/api/stats` | 各状态计数 |
| GET | `/health` | 存活探针（免认证） |

认证：`Authorization: Bearer <token>`（或 `X-Draftgate-Token`）。
条目一旦决策即不可更改——决策是终态且幂等的。

`content` 在 `edit` 时必填；`reason` 三种动作都可选，会原样出现在 webhook 的
`reason` 字段里（未填为 null）。用它告诉自动化管线**为什么**被拒，
重试时才有依据可循。

## n8n 集成

**方式 A — Execute Command 节点**（最简单）：

1. `draftgate submit --title "..." --file out.txt --timeout 900`
2. 按节点退出码分支（`0` / `1` / `2`）
3. 解析 stdout 的 JSON 拿最终内容

**方式 B — HTTP Request 节点**：POST 到 `/api/items`，然后定时轮询
`GET /api/items/{id}` 直到 `status != "pending"`。

记得调大节点超时——`submit --timeout` 是有意阻塞的。

## 配置

| 环境变量 | 默认值 | 用途 |
|---|---|---|
| `DRAFTGATE_TOKEN` | 首次运行自动生成 | API token |
| `DRAFTGATE_SECRET` | 首次运行自动生成 | 默认 webhook 签名密钥 |
| `DRAFTGATE_DB` | `~/.draftgate/draftgate.db` | SQLite 文件位置 |
| `DRAFTGATE_HOME` | `~/.draftgate` | 配置目录 |

条目级 `callback_secret` 会覆盖全局密钥。

## 安全说明

- 默认只绑定 `127.0.0.1`；绑定其他地址会被拒绝，除非显式设置 `DRAFTGATE_TOKEN`
  （首次运行自动生成的那个不算——那等于把一个你自己都没见过的 token 当成唯一防线）
- CORS 仅放行 localhost 来源
- UI 把 token 存在浏览器 `localStorage`——单用户本机使用没问题，共用电脑请勿使用
- webhook 密钥明文存在本地 SQLite；需要轮换就重新提交条目

## 开发

```bash
git clone https://github.com/buxiangcifan123/draftgate.git
cd draftgate
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"   # Windows
# .venv/bin/pip install -e ".[dev]"                             # macOS / Linux

pytest                     # 单元测试
python tests/e2e_demo.py   # 端到端：服务 + CLI + webhook
```

## 路线图

- [ ] PyPI 发布
- [ ] 长轮询等待端点（`GET /api/items/{id}/wait`）
- [ ] Docker 镜像
- [ ] 界面多语言
- [ ] 多审核人 / 审计日志

## 许可证

[MIT](LICENSE)
