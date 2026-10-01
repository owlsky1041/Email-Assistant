# 腾讯企业邮箱邮件管理助手

单机运行的邮件归档 + 知识库检索助手。从腾讯企业邮箱（IMAP）增量拉取邮件，
按原始文件夹结构归档为 Markdown，附件存本地，元数据入 SQLite，
正文切片并向量化，对外提供本地知识库 API 供智能体 / RAG 系统调用。

```
腾讯企业邮箱 ──IMAP──▶ 解析/清洗 ──▶ Markdown + 附件（本地归档）
                            │
                            └──▶ SQLite（WAL + FTS5）──▶ 切片 ──▶ 向量库
                                                              │
                                       智能体 ◀── FastAPI ──────┘
                                             127.0.0.1:8990
```

---

## 快速开始

### 1. 安装

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate

pip install -r requirements.txt
```

核心依赖装完即可运行（关键词检索 + 哈希兜底向量检索）。
**语义检索需要可选依赖**，见下方「启用真正的语义检索」。

### 2. 初始化配置

```bash
python main.py init
```

生成的 `config/config.yaml` 带完整中文注释。至少填写：

```yaml
email:
  address: "you@yourcorp.com"
```

> **授权码不会写进配置文件。** 配置里只有 `auth_code_env`（环境变量名）
> 和 `auth_code_ref`（本地密钥库键名）。

### 3. 设置授权码

在 `腾讯企业邮箱 → 设置 → 客户端设置` 中开启 IMAP 并生成授权码，然后：

```bash
python main.py auth set      # 输入不回显
```

或使用环境变量（推荐用于服务器 / CI）：

```bash
export EMAIL_ASSISTANT_AUTH_CODE='你的授权码'
```

### 4. 自检

```bash
python main.py doctor               # 环境与配置
python main.py doctor --check-imap  # 顺便测试邮箱连通性
```

### 5. 打开设置界面

```bash
python main.py settings
```

弹出**原生设置窗口**（tkinter 实现，无需浏览器、无需敲命令），四个分页：

| 分页 | 可配置项 |
|---|---|
| **邮箱接入** | 邮箱账号、IMAP 服务器、端口、SSL、**客户端授权码**、**测试连接**（只读验证并回显文件夹数与 INBOX 封数） |
| **数据存放** | 数据根目录（含"浏览…"原生目录选择框）+ 各路径实时预览；归档、附件、数据库、向量库、备份、模型全部由这一个根目录派生 |
| **同步** | 文件夹勾选（可从服务器拉取真实列表）、排除文件夹、同步间隔、并发下载连接数、分批大小、附件大小上限、单次封数上限、下载附件与删除比对开关 |
| **检索模型** | 嵌入后端、模型状态、ONNX 仓库与下载源、一键下载（带进度条）或从本地目录导入 |

打包版（Windows/Linux）双击启动后，若尚未配置邮箱，程序会**自动弹出这个原生窗口**；
也可以随时从**托盘右键 →「设置…」**打开。

> `python main.py settings --browser` 仍可使用旧的网页设置页
> （含同步进度面板、数据迁移等高级功能），无图形界面的服务器环境会自动回退到它。

### 6. 开始使用

```bash
python main.py sync                 # 手动同步一次
python main.py status               # 查看同步状态与水位
python main.py search "报销发票"     # 混合检索
python main.py serve                # 启动知识库 API
python main.py tray                 # 托盘常驻 + 定时同步
```

不想连真实邮箱也可以先体验：

```bash
python scripts/seed_demo.py --reset
```

---

## 设置界面

不需要改 YAML、也不需要记命令。默认是**原生窗口**；加 `--browser` 才是下面的网页设置页
（功能更全，但需要开浏览器）：

| 分组 | 可配置项 |
|---|---|
| **同步进度** | 实时进度条、当前文件夹与封数、速度与剩余时间、滚动日志、一键「立即同步」/「全量重新扫描」 |
| **邮箱账户** | 邮箱地址、IMAP 服务器、端口、SSL、**授权码**、测试连接 |
| **数据存放位置** | 数据根目录（含"浏览…"原生目录选择框）、各路径高级覆盖、**一键迁移已有数据** |
| **同步设置** | 同步间隔、附件大小上限、单次上限、**并发下载连接数**、文件夹勾选、排除目录 |
| **检索与语义搜索** | 嵌入后端、向量库后端、ONNX 模型目录与就绪状态 |
| **本地知识库服务** | 监听地址、端口、访问令牌 |
| **通知** | 新邮件气泡开关、点击行为、企业邮箱网页地址 |

### 安全模型

设置接口能**改写配置并写入授权码**，比只读的知识库接口危险得多 ——
一个恶意网页只要能让浏览器向 `127.0.0.1:8990` 发请求就可能篡改配置。
因此叠加了三重防护：

1. **强制回环**：`api.host` 一旦不是回环地址，设置接口**直接不注册**
   （而不是仅告警）。把知识库 API 暴露到局域网时，写配置的能力必须一并消失。
2. **一次性令牌**：进程启动时随机生成并注入设置页；跨站脚本因同源策略
   读不到响应体，拿不到令牌，且令牌随进程退出失效。
3. **同源校验**：拒绝携带跨站 `Origin` 或 `Sec-Fetch-Site: cross-site` 的请求。

授权码**只写不读**：接口只返回"是否已配置/来源/长度"，绝不回显明文，
也不写入配置文件。

### 同步进度

同步跑在后台线程，界面轮询 `/api/sync/progress` 显示：

* 进度条、当前文件夹、`已下载/总数`、下载速度（封/秒）与预估剩余时间
* 累计归档/跳过/失败/删除数
* 滚动日志（归档到"主题"级别，失败带错误摘要）
* 每个文件夹的结果汇总

进度状态对象是线程安全的（并发下载时计数不会串），事件历史有界（最多 200 条），
且只保存摘要信息，不缓存邮件正文。

### 并发下载

```yaml
sync:
  fetch_workers: 3     # 1 = 顺序下载；建议 3-5，过多会被服务端限流
```

**关键约束：IMAP 连接不能跨线程共享。** `imaplib` / `imap-tools` 的连接对象
并非线程安全，同一条连接上并发发命令会导致响应错位（取到别人的邮件或解析异常）。
因此并发下载采用**每线程一条独立连接**的连接池，而不是共享连接：

* 连接按需创建 —— 只有真正干活的线程才建连，顺序模式（`fetch_workers: 1`）零额外开销；
* 数据库写入由 `Database` 内部写锁串行化，WAL 模式下安全；
* 文件写入互不重叠，可安全并发；
* 某个线程建连失败只影响它自己那一封，不会拖垮整批。

实测（30 封邮件、4 并发）功能与顺序下载完全一致：无重复、无丢失，
水位推进正确，且"单次上限截断"时水位仍只停在实际处理过的 UID 上。

## 命令行参考

| 命令 | 说明 |
|---|---|
| `init` | 生成配置模板 / 交互式配置向导 |
| `auth set\|clear\|status\|show` | 管理授权码（密钥库 / 环境变量） |
| `doctor` | 环境、依赖、数据库、目录、IMAP 全面自检 |
| `sync [--folder X] [--full] [--no-index]` | 手动同步 |
| `status [--json]` | 同步状态与每个文件夹的水位 |
| `folders` | 列出服务端文件夹 |
| `search "查询" [--mode hybrid\|keyword\|vector]` | 检索邮件 |
| `index [--rebuild] [--limit N]` | 生成 / 重建切片与向量索引 |
| `serve [--host H] [--port P]` | 启动知识库 API |
| `settings [--browser]` | 打开原生设置窗口（`--browser` 用网页设置页） |
| `tray [--no-api]` | 托盘常驻 + 定时同步 + 通知 |
| `backup [--with-files]` / `restore FILE` | 备份 / 恢复 |
| `rebuild-fts` | 重建全文索引 |
| `config [--show]` | 查看配置 |
| `demo [--count N] [--reset]` | 生成演示数据（无需真实邮箱） |
| `model status\|import\|download` | 管理 ONNX 嵌入模型 |

`python main.py` 不带参数等价于 `tray`；无图形界面时自动降级为纯守护模式。

---

## 知识库 API

默认监听 `127.0.0.1:8990`，文档在 <http://127.0.0.1:8990/docs>。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查（免鉴权） |
| GET | `/api/sync/status` | 同步状态 |
| POST | `/api/sync/trigger` | 手动触发同步（后台线程执行） |
| GET | `/api/messages` | 邮件列表（分页 + 过滤） |
| GET | `/api/messages/{id}` | 单封邮件元数据（`id` 可为 `messages.id` 或 Message-ID） |
| GET | `/api/messages/{id}/content` | Markdown 正文 |
| GET | `/api/attachments/{id}` | 附件本地路径 |
| POST | `/api/search` | 关键词 + 向量混合检索 |
| POST | `/api/embed` | 文本向量化 |
| GET | `/api/chunks/{message_id}` | 邮件切片 |
| GET | `/api/stats` | 知识库统计 |

### 鉴权

`api.token` 为空时不鉴权（仅回环地址可达）；设置后所有业务接口都需要：

```bash
curl -H "Authorization: Bearer $EMAIL_ASSISTANT_API_TOKEN" \
     http://127.0.0.1:8990/api/messages
```

### 调用示例

```bash
curl -X POST http://127.0.0.1:8990/api/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"报销发票怎么弄","mode":"hybrid","limit":5}'
```

```json
{
  "query": "报销发票怎么弄",
  "mode": "hybrid",
  "count": 2,
  "results": [
    {
      "message_id": "demo-1@corp.com",
      "subject": "季度报销发票汇总",
      "sender": "爱丽丝 <alice@corp.com>",
      "date": "2024-03-01T09:23:00+00:00",
      "folder": "INBOX",
      "snippet": "本季度差旅报销发票已整理完毕，请财务审核。",
      "local_markdown_path": "/…/INBOX/20240301_092300_季度报销发票汇总_1.md",
      "score": 0.0328,
      "source": "hybrid",
      "keyword_rank": 1, "vector_rank": 1
    }
  ]
}
```

---

## 启用真正的语义检索

默认使用内置的 `hashing` 兜底后端（**零依赖、离线可跑**，但只是词法相似度，
不是语义相似度）。生产环境请启用 ONNX 后端。

### 1. 安装导出工具并导出模型

导出需要 PyTorch，但**运行时不需要**。为避免拉取数 GB 的 CUDA 依赖，
请显式安装 CPU-only 版：

```bash
# CPU-only torch（约 200MB，PyPI 默认版本会带上 2-3GB 的 CUDA 依赖）
pip install --index-url https://download.pytorch.org/whl/cpu "torch>=2.2"
pip install optimum-onnx

# HuggingFace 不可达时用镜像
export HF_ENDPOINT=https://hf-mirror.com
optimum-cli export onnx \
  --model BAAI/bge-small-zh-v1.5 \
  --task feature-extraction \
  ./data/models
```

导出后 `data/models/` 应包含 `model.onnx`（约 95MB）与 `tokenizer.json`。

如果用发行包（未自带模型），可以走更省事的两条路：

```bash
# 从发行包附件或内部镜像下载
./email-assistant model download --url <模型zip的下载地址>
# 或从已有目录导入（离线/内网）
./email-assistant model import /path/to/model-dir
./email-assistant model status     # 确认是否就绪
```

> ⚠️ 公开仓库里的第三方 ONNX 导出，**池化方式可能与
> sentence-transformers 不一致**。实测 `Xenova/bge-small-zh-v1.5` 的
> 相关/无关句对相似度几乎不可分（0.287 vs 0.283），不能直接用。
> 因此推荐用本项目自己导出的模型，或在下载第三方模型后先用
> `scripts/calibrate_threshold.py` 验证区分度。

> 导出完成后 `torch` / `transformers` / `optimum` 就不再被程序使用了。
> 如需精简虚拟环境（可省约 900MB）：
> ```bash
> pip uninstall -y torch transformers optimum optimum-onnx sympy networkx
> ```

### 2. 配置

```yaml
embedding:
  backend: "onnx"        # auto 也会优先尝试 onnx
  model_dir: "./data/models"
vector:
  backend: "chroma"      # 或 sqlite-vec / sqlite-bruteforce
```

### 3. 重建索引并验证

```bash
python main.py index --rebuild
python main.py doctor        # 应显示「嵌入后端 —— onnx / 512 维」
```

切换嵌入模型后**必须**重建索引，因为旧向量与新模型的向量空间不可比。

### 实测效果

用 `BAAI/bge-small-zh-v1.5` + ChromaDB，在**查询词与文档零字面重叠**的
情况下（下面每个查询都不含文档中的任何关键词）：

| 查询 | 命中文档 | 余弦 |
|---|---|---|
| 出差费用怎么报 | 季度报销发票汇总 | 0.554 |
| 机器不够用了想加几台 | 服务器扩容申请 | 0.547 |
| 公司被黑客攻击的风险 | 网络安全加固说明 | 0.397 |
| 我今年表现怎么样 | 年度绩效考核通知 | 0.501 |
| 结算流程需要改 | 产品需求评审会议纪要 | 0.500 |

5/5 全部命中正确文档，且都排在第一位。

### 关于相似度阈值的诚实说明

纯向量检索**永远**会返回"最近的邻居"，即使它们毫不相关。
而单一绝对阈值无法可靠区分相关与无关——实测该模型的中文语料分布：

```
相关查询 top1 ∈ [0.39, 0.57]
无关查询 top1 ∈ [0.33, 0.45]     ← 存在重叠区
```

因此程序的策略是：

* 默认阈值为 **0（自适应）**，沿用嵌入后端的推荐值
  （`hashing` 0.25；bge 系列 0.35）——避免切换后端时召回行为被全局魔数悄悄改变；
* 调小降噪、调大召回，由 `search.min_vector_score` 显式覆盖；
* `search.vector_score_margin` 可按「比最佳命中低多少」裁掉每次查询的长尾；
* **检索结果始终返回 `vector_score`**，调用方（智能体）应据此判断可信度。

请用自己的邮件语料标定阈值：

```bash
python scripts/calibrate_threshold.py
# 或提供自己的查询集（TAB 分隔 类别+文本）
python scripts/calibrate_threshold.py --queries my_queries.txt
```

脚本会输出两类查询的分布，存在分离区间时给出建议值，
存在重叠时如实说明并给出「保召回 / 优先降噪」两套取值。


### 后端矩阵

| 组件 | 可选值 | 说明 |
|---|---|---|
| 嵌入 | `onnx` | **推荐**，约 200MB 依赖，无需 PyTorch |
| | `sentence-transformers` | 兼容性最好，但需完整 PyTorch（~2GB） |
| | `hashing` | 零依赖兜底，仅词法相似度，用于开发 / CI |
| 向量库 | `chroma` | 计划书首选，持久化到 `data/chromadb` |
| | `sqlite-vec` | SQLite 原生向量扩展，需 `pip install sqlite-vec` |
| | `sqlite-bruteforce` | 全量点积，万级邮件完全够用 |

`backend: "auto"` 会按 `onnx → sentence-transformers → hashing` 与
`chroma → sqlite-vec → sqlite-bruteforce` 的顺序自动降级，并把降级原因写进日志。

---

## 目录结构

```
email-assistant/
├── config/config.yaml          # 配置（授权码不在这里）
├── data/
│   ├── mail_archive/           # Markdown 归档，保持邮箱文件夹层级
│   │   └── <账号>/<文件夹>/YYYYMMDD_HHmmss_主题_UID.md
│   │                        └── attachments/
│   ├── sqlite/mail.db          # 元数据 + FTS5 + 切片 + 向量
│   ├── chromadb/               # ChromaDB 持久化目录
│   ├── models/                 # ONNX 模型
│   └── backups/
├── logs/                       # 轮转日志（已脱敏）
├── src/
│   ├── config.py               # 配置模型与校验
│   ├── config_writer.py        # 配置模板与写回
│   ├── secret_store.py         # 授权码安全存储
│   ├── logging_setup.py        # 日志与脱敏
│   ├── models.py               # 领域模型
│   ├── utils.py                # 文件名安全化 / 原子写入 / Token 估算
│   ├── cleaner.py              # HTML 清洗 + 噪音过滤
│   ├── gui/                    # 原生设置窗口（tkinter，零额外依赖）
│   │   ├── settings_model.py   #   草稿模型/校验/保存/连通性测试（可无头测试）
│   │   └── settings_window.py  #   tkinter 窗口与子进程入口
│   ├── mail_parser.py          # MIME 解析（中文头部 / RFC2231）
│   ├── imap_client.py          # IMAP 封装（BODYSTRUCTURE 预检、分页、重连）
│   ├── markdown_exporter.py    # Markdown + 附件落盘
│   ├── chunker.py              # 语义单元装箱切片
│   ├── database.py             # SQLite（WAL / FTS5 / 迁移）
│   ├── embedder.py             # 嵌入后端（onnx / ST / hashing）
│   ├── vector_store.py         # 向量库（chroma / sqlite-vec / bruteforce）
│   ├── indexer.py              # 切片 → 嵌入 → 向量（增量复用）
│   ├── search.py               # 关键词 + 向量 + RRF 融合
│   ├── sync_service.py         # 同步编排
│   ├── scheduler.py            # APScheduler 定时任务
│   ├── kb_api.py               # FastAPI 服务
│   ├── settings_api.py         # 设置接口（令牌 + 同源 + 回环三重防护）
│   ├── settings_service.py     # 设置读写、校验、连接测试、数据迁移
│   ├── progress.py             # 线程安全的同步进度状态
│   ├── webui/settings.html     # 设置界面（单文件，无外部依赖）
│   ├── tray_app.py             # 托盘常驻与通知
│   ├── cancellation.py         # 全局取消令牌
│   └── cli.py                  # 命令行
├── packaging/
│   ├── email-assistant.spec    # PyInstaller 配置（三平台通用）
│   ├── build_linux.sh          # Linux 构建（含验收测试）
│   ├── build_macos.sh          # macOS 构建（含签名/公证）
│   ├── build_windows.ps1       # Windows 构建
│   └── installer.iss           # Inno Setup 安装程序脚本
├── scripts/
│   ├── seed_demo.py            # 演示数据生成
│   └── calibrate_threshold.py  # 用你自己的语料标定相似度阈值
├── .github/workflows/build.yml # 三平台 CI 构建矩阵
├── tests/                      # 653 个测试
└── main.py
```

---

## 工程约束的实现说明

计划书第 11 章列出的硬性约束，逐条落实如下。

### 11.1 进程与并发模型

| 要求 | 实现 |
|---|---|
| UI 主线程不做网络 / IO / 推理 | `tray_app.py` 的菜单回调只投递线程；同步由 `SyncScheduler` 工作线程执行 |
| API 服务隔离 | `TrayApplication.start_api_process()` 用**独立子进程**跑 uvicorn，与 GUI 事件循环零共享 |
| SQLite 开启 WAL | `Database._configure()` 执行 `PRAGMA journal_mode=WAL` + `busy_timeout=30000`；每线程独立连接 |

### 11.2 IMAP 与解析防御性编程

| 要求 | 实现 |
|---|---|
| 不用原生 `imaplib` 处理中文邮件 | 基于 `imap-tools` 封装；仅用底层 `uid()` 做 `RFC822.SIZE` 预检与分部分拉取 |
| 分批拉取 | `sync.fetch_batch_size` 分批，批间检查取消令牌 |
| 附件流式写入 | `ChunkedFileWriter` 分块写 + 大小校验 + SHA-256 |
| `.tmp` 临时文件机制 | 所有落盘先写 `.tmp` 再原子 `os.replace`；失败自动清理，绝不留下半成品 |
| 增量 + 定期全量 UID 比对 | `folders.last_uid` 增量水位；每 `full_scan_interval_hours` 拉全量 UID 比对，处理网页端删除/移动 |
| 超大附件保护 | 下载前用 `BODYSTRUCTURE` 判断分部大小，超阈值只记录元数据；正文仍会通过单独拉取文本分部保留 |

> **注意**：`--full` 与单次上限（`max_messages_per_run`）互斥时，水位只会推进到
> **实际处理过的**最高 UID，确保断点续传不丢邮件。

### 11.3 邮件清洗与 RAG 数据质量

| 要求 | 实现 |
|---|---|
| HTML 深度清洗 | `clean_html()` 移除 `script`/`style`/隐藏元素/1×1 追踪像素/事件属性 |
| 内联图片处理 | 解析 `Content-Disposition: inline` + `Content-ID`，把 HTML 里的 `cid:` 换成落盘后的**真实相对路径** |
| 噪音过滤 | 切除 RFC 3676 签名分隔符、中英文回复引用头、Outlook「原始邮件」标记、移动端签名、法务免责声明；**默认保留转发/引用历史**（`clean.strip_quoted_history`），只在邮件末尾 30% 区域内找标记，避免历史正文被连带截断 |
| 正文保底 | 噪音过滤后若正文剩余不足 25%（`_body_mostly_lost`），自动退化为保守清理，宁可留下噪音也不丢正文 |
| 纯文本降级 | `_markdown_to_plain()` 先寄存 Markdown 反斜杠转义再清理强调符号，避免 `zj\_foo@corp.com` 被破坏成 `zj\foo@corp.com`；同时剥掉 `> > >` 引用层级标记，提升检索片段可读性 |

### 11.4 检索算法与资源控制

| 要求 | 实现 |
|---|---|
| RRF 融合 | `reciprocal_rank_fusion()`：`score(d) = Σ w_r / (k + rank_r(d))`，`k=60`，无需分数归一化 |
| 模型轻量化 | 优先 `onnxruntime`（约 200MB），避免完整 PyTorch |
| 优雅退出 | `CancellationToken` 贯穿 IMAP 拉取、嵌入推理、批量入库；`install_signal_handlers()` 拦截 SIGINT/SIGTERM/SIGBREAK，确保事务提交与文件完整 |

---

## 安全与隐私

* 授权码**不写入**配置文件，**不写入**日志（`RedactingFilter` 对所有日志记录做
  key-value / Bearer / 长令牌三重脱敏），**不通过** API 返回。
* 存储优先级：环境变量 → 系统密钥库（`keyring`）→ 本地 Fernet 加密文件。
  加密文件的密钥与密文同机存放，仅防误读；服务器环境请用环境变量。
* API 默认只监听 `127.0.0.1`；改成非回环地址时启动会打印显著告警，
  并且**设置界面会被自动停用**（它可改写配置与授权码）。
* 日志不记录完整邮件正文，超长内容自动截断。
* 数据库、归档目录、附件默认权限 `0700`，归档文件 `0600`。
* 附件名与文件夹名统一经过 `sanitize_filename`，阻断路径穿越与 Windows 保留名。
* `data/`、`logs/`、`config/config.yaml`、`.secrets.*` 已加入 `.gitignore`。

---

## 测试

```bash
python -m pytest              # 653 个测试，约 16 秒
python -m pytest -m slow      # 需要真实模型/网络的测试
```

覆盖范围包括：中文 FTS5 检索（含自然语言问句的渐进放宽）、RRF 融合数学、
MIME 解析（GBK / RFC2231 / 内联图）、BODYSTRUCTURE 解析、噪音清洗、切片重叠、
附件原子写入、增量嵌入复用、同步去重与水位、UIDVALIDITY 变化、超大附件、
全量比对、断点续传、API 全集与鉴权、日志脱敏、路径穿越防护、配置写回、
ONNX 推理链路（用合成模型，无需下载真实模型）。

需要真实模型或第三方库的测试（ChromaDB、ONNXRuntime、托盘）在依赖缺失时
自动 skip，不会造成失败。

同步相关测试使用 `tests/conftest.py` 中的 `FakeImapClient`，
**无需真实邮箱即可完整验证增量同步语义**。

---

## 打包为桌面应用

### 先说清楚：不能交叉编译

PyInstaller / Nuitka 都不是交叉编译器——它们把**当前平台**的原生解释器和
二进制打进包里。因此：

| 目标平台 | 必须在哪构建 | 当前是否交付 |
|---|---|---|
| Windows `.exe` / 安装程序 | Windows | ✅ **主要目标** |
| Linux `tar.gz` / AppImage | Linux | ✅ 用于验证打包链路 |
| macOS `.app` / `.dmg` | macOS（签名公证还需 Apple 账号） | ⏸ 已搁置 |

这与有没有 .NET 环境无关（.NET 的跨平台发布只适用于 .NET 程序，
本项目是 Python + FastAPI + onnxruntime + chromadb）。

**拿到产物的两条路：**

1. **CI（推荐）**——`.github/workflows/build.yml` 用 GitHub 原生 runner
   并行构建 `windows-x64` 与 `linux-x86_64`，打 tag 时自动创建 Release。
2. **本地构建**——在对应系统上运行：

```bash
packaging\build_windows.ps1       # Windows (PowerShell)
./packaging/build_linux.sh        # Linux
```

> macOS 已搁置：`packaging/build_macos.sh` 保留但未纳入 CI、未做真机验证，
> 官方 Release 不含 macOS 产物。

### 产物

| 平台 | 文件 | 说明 |
|---|---|---|
| **Windows** | `EmailAssistant-<版本>-windows-setup.exe` | Inno Setup 安装程序（含开始菜单/开机启动选项） |
| **Windows** | `email-assistant-<版本>-windows-x64.zip` | 绿色免安装 |
| Linux | `EmailAssistant-<版本>-linux-x86_64.AppImage` | `chmod +x` 直接运行 |
| Linux | `email-assistant-<版本>-linux-x86_64.tar.gz` | 解压即用 |
| 全部 | `bge-small-zh-v1.5-onnx.zip` | 独立的 ONNX 模型附件 |

每个包内都附带 `快速上手.txt`（中文入门指引）。

### 实测数据（Linux x86_64）

```
dist/email-assistant/                     334 MB   onedir 目录
dist/email-assistant-<版本>-linux-x86_64.tar.gz   146 MB
```

打包产物**自带 Python 运行时与全部依赖**，目标机器无需安装 Python。
构建脚本会在隔离环境（`env -i`）里跑一遍验收测试，关键能力（SQLite、
FTS5、IMAP 库、FastAPI、加密）有一项不通过就中止打包。

### 数据目录

| 场景 | 位置 |
|---|---|
| 源码运行 | 项目根目录 |
| Windows / Linux 打包版 | **可执行文件所在目录**（绿色版习惯） |
| 任意场景 | 环境变量 `EMAIL_ASSISTANT_HOME` 可整体重定位 |

> macOS 若将来恢复，数据目录需放到 `~/Library/Application Support/EmailAssistant/`：
> 应用包签名后只读，且升级时整包替换，把用户数据放进去会直接丢失。
> 该分支逻辑已在 `runtime_root()` 中实现，但未验证。

### 关于 ONNX 模型

模型（95MB）**不默认打进发行包**，否则每个平台的安装包都要膨胀近一倍。
发行时会作为独立附件提供：

```bash
# 下载模型附件后导入
./email-assistant model import /path/to/bge-small-zh-v1.5-onnx
./email-assistant index --rebuild
```

CI 里也可以勾选 `bundle_model` 把模型直接打进包里（开箱即用，但包体翻倍）。

### macOS（已搁置）

`packaging/build_macos.sh` 与 spec 里的 `BUNDLE` 段仍然保留，
但**未纳入 CI、未做真机验证，也未经签名/公证测试**，官方 Release 不含 macOS 产物。
将来恢复时至少要补齐：真机完整跑通、`.app` 内设置界面可打开、
Developer ID 签名 + notarytool 公证。

### 打包时的几个坑（已修）

这些是实际构建时才暴露的问题，记录在此以免重复踩：

1. **`PROJECT_ROOT` 冻结后错位**——PyInstaller 把模块放进 `_internal/`，
   按 `__file__` 推导会把用户配置和数据埋进包体内部。已改为认
   `sys.executable` 所在目录（macOS 例外，见上）。
2. **chromadb 整个 import 不了**——`chromadb/__init__.py` 会导入
   `chromadb.auth.token_authn`，它依赖 `opentelemetry`。spec 里把它
   排除掉会导致 chromadb 直接不可用。`grpc` / `kubernetes` / `boto3` 同理。
3. **托盘被静默丢弃**——用 `__import__` 判断"是否装了 pystray"在无显示
   环境下会抛 `Xlib.error.DisplayNameError`，导致 CI 构建出来的包没有托盘。
   已改用 `importlib.util.find_spec` 判断"是否存在"。
4. **构建机数据混进发行包**——验收测试若没隔离好，会把
   `config/config.yaml` 和 `data/sqlite/mail.db` 打进包里。构建脚本现在
   显式清理，且用 `env -i` 传独立的 `EMAIL_ASSISTANT_HOME`。
5. **Debian/Ubuntu 缺 `libpython`**——系统 Python 是静态构建时
   PyInstaller 会报 `Python shared library not found`。
   `apt install libpython3.13` 即可。
6. **PyPI 的 torch 默认带 CUDA**——导出 ONNX 时若直接
   `pip install optimum[exporters]` 会拉 2-3GB 的 CUDA 依赖。
   必须用 `--index-url https://download.pytorch.org/whl/cpu`。
7. **`env -i` 会清空环境**——`VAR=x env -i PATH=... cmd` 里的 `VAR`
   会被丢掉，必须写成 `env -i VAR=x PATH=... cmd`。

---

## 后续扩展预留

* **PostgreSQL**：`database.py` 的迁移表结构与 DAO 已按可替换设计，`MIGRATIONS`
  列表可平滑增加方言分支。
* **FAISS / sqlite-vec**：`VectorStore` 是抽象基类，新增后端只需实现
  `upsert / query / delete / count / reset`。
* **多账户**：`messages.account`、`folders.account` 已作为主键一部分；
  `storage.per_account_subdir` 控制归档是否按账号分层。
* **邮件标签与分类**：`messages` 表可扩展 `labels` 关联表。
* **导出 Obsidian / Notion**：归档已是纯 Markdown + frontmatter，可直接作为
  Obsidian vault；Notion 侧只需遍历 `messages` 调其 API。
* **RAG 智能体**：直接调用 `POST /api/search` 与 `GET /api/chunks/{id}`。
