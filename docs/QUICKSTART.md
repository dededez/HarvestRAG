# Web-Scrapper V2 启动与部署

默认部署为单机 Docker Compose，多个 Worker 共用 PostgreSQL、Milvus、模型缓存与产物卷。没有 Benchmark。
当前这台开发机的 PostgreSQL/Milvus 已按 `local-milvus.md` 启动；正式环境仍以下面的 Compose 为准。

## 本机 Python 开发

在项目根目录运行（Windows PowerShell）：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-v2.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

需要已有 PostgreSQL 和 Milvus 2.6 服务。将以下配置写入环境变量，或合并至现有 `.env`。
不要用示例覆盖现有 API Key。

```powershell
$env:V2_DATABASE_URL = 'postgresql://scraper:YOUR_PASSWORD@127.0.0.1:5432/scraper'
$env:V2_MILVUS_URI = 'http://127.0.0.1:19530'
$env:V2_API_KEY = 'YOUR_RANDOM_KEY'
$env:V2_ARTIFACT_DIR = 'D:/scraper-data/artifacts'
.\.venv\Scripts\python.exe -m v2 init
.\.venv\Scripts\python.exe -m v2 serve
```

另一个终端设置相同环境变量，然后启动 Worker：

```powershell
.\.venv\Scripts\python.exe -m v2 worker
```

访问 http://127.0.0.1:8000 ，填写访问密钥并提交任务。API 文档在 `/docs`。
控制台分为 5 个视图：采集（手动/自主发现）、问答（知识库/按 URL）、任务（进度/审核/重试/停止）、日志（过滤/自动刷新/清空）、数据（语料指标/删除 URL）。
`/health` 只代表应用存活；`/ready` 检查 PostgreSQL/Milvus，不表示模型已经下载或联调完成。
模型首次使用会下载，BGE-M3 与 Reranker 需要额外内存、磁盘和启动时间；不会静默换用另一种 Embedding。

嵌入模型显式回退（V1 行为，默认开启）：

```powershell
$env:V2_ALLOW_EMBEDDING_FALLBACK = 'true'
$env:V2_EMBEDDING_FALLBACK_MODEL = 'all-MiniLM-L6-v2'
```

回退会写 `embedding_fallback` 事件并改变处理指纹；已有 Job 不会自动复用。

## 本机 WSL Milvus（当前机器，非正式部署）

本机 PostgreSQL 5433 由 `knowledge/start-local-postgres.ps1` 启动（`Start-Process` 脱离工具进程树）；
一键启动 PostgreSQL + Milvus：`knowledge/start-local-services.ps1`。

本机没有 Docker Desktop，Milvus 2.6.0 跑在独立 WSL 发行版 `milvus` 中；MinIO 是该发行版内的原生进程。
启动脚本可重复执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File <协作工作区>\knowledge\start-local-milvus.ps1
```

19530 通过 WSL2 localhost 映射到 Windows。组成、停止、备份与重建步骤见 `local-milvus.md`。

## Docker Compose（稍后执行）

先准备 Docker Desktop 的 Linux 容器环境，或 Linux Docker 主机。

```powershell
Copy-Item .env.v2.example .env.v2
```

编辑 `.env.v2`：设置独立的 `POSTGRES_PASSWORD`、`V2_API_KEY`，需要 LLM 时配置
`config/agents.json` 对应的环境变量。数据库密码建议使用随机字母数字，避免 URL 编码问题。
自主搜索另需 `DEEPSEEK_API_KEY`。手动 URL + 结构解析 + 检索不需要 LLM API Key。

```powershell
docker compose --env-file .env.v2 -f compose.v2.yaml config --quiet
docker compose --env-file .env.v2 -f compose.v2.yaml up -d --build
docker compose --env-file .env.v2 -f compose.v2.yaml logs -f api worker
```

首次启动须下载镜像、Python 依赖、浏览器与模型；本轮未执行镜像构建或服务启动。
Compose 仅将控制台绑定至宿主机 127.0.0.1:8000；PostgreSQL、Milvus、etcd、MinIO 不开放宿主机端口。
MinIO 是 Milvus 自身的存储依赖；阶段产物仍保存在共享 `artifacts` 卷。
远程访问使用你的现有反向代理与访问控制。当前版本是单用户可信操作台，不是多租户 SaaS。

增加同机 Worker：

```powershell
docker compose --env-file .env.v2 -f compose.v2.yaml up -d --scale worker=2
```

先用一个 Worker 完成部署验收再增加数量；每个进程独立加载模型，内存消耗相应增加。
API 和 Worker 必须采用相同代码、模型、提取配置和产物卷。跨机器须先实现共享产物存储，不可直接使用各机本地目录。

## 常用操作

```powershell
# 仅采集给定页面
python -m v2 submit https://docs.python.org/3/ --max-pages 1

# 有界发现：只访问种子所属域名
python -m v2 submit https://docs.python.org/3/ --max-pages 20 --depth 1 --discover links+sitemap

# LLM 提取是显式选项
python -m v2 submit https://example.com/article --extract llm

# 搜索并生成计划，不自动提交；可以检查、修改 JSON
python -m v2 plan "Python 官方异步编程文档" --output plan.json
python -m v2 submit --plan plan.json

python -m v2 status
python -m v2 query "如何创建异步任务？"
python -m v2 query "如何创建异步任务？" --answer
python -m v2 retry TASK_ID
python -m v2 cleanup

# V1 风格批量文件（--reset 清理这些 URL 的旧任务；--workers 启动多个 Worker 子进程）
python -m v2 run urls.txt --reset --workers 2 --extract structured

# 单步：抓取 / 清洗 / 提取 / 分块
python -m v2 fetch https://example.com/
python -m v2 strip page.html --url https://example.com/
python -m v2 extract --url https://example.com/ --file page.html
python -m v2 chunk extraction.json

# 每 URL 实时问答（语料优先，未命中现场抓取；--answer 生成回答）
python -m v2 live https://example.com/ --question "这个页面讲了什么？" --answer

# 删除 URL / 取消 Job / 日志
python -m v2 delete https://example.com/
python -m v2 cancel JOB_ID
python -m v2 events --url example.com --event fetch_attempt --limit 50
python -m v2 events --clear
```

容器中执行 CLI，可在命令前使用 `docker compose --env-file .env.v2 -f compose.v2.yaml exec worker`。
同一 `job_id` 和相同计划的重复提交幂等；要主动刷新页面，应创建新 Job。
重试接口只接受已终止的失败/待审核/阻塞任务，保留 FETCH 产物并重新执行后续步骤；
要重新访问网站或采用新配置，请提交新 Job。

## V1 数据迁移与回退

V1 原有 SQLite、Chroma 和 `.env` 不会被 V2 改写。迁移采取读取已完成 URL、重新采集入库：

```powershell
python -m v2 import-v1 data/queue.db
```

这是**重新采集迁移**，不是旧向量逐字节搬迁。已经离线或需要保留历史原文的页面，不应仅依赖此命令；
应先保留 V1 数据备份，再单独设计离线导入。V2 验证通过前不要删除 V1 数据。
需要回退时停止 V2 API/Worker，并继续使用原 `streamlit run ui/app.py`。

## 检索阈值与评估

`V2_RERANK_MIN_SCORE` 默认 **0.20**（仅在启用 reranker 时生效）；低于阈值的证据不会进入回答，
全部被过滤时 `/query` 返回 `No sufficiently relevant evidence found`。

用小评估集复核阈值（库内 8 题 + 库外 5 题）：

```powershell
$env:CUDA_VISIBLE_DEVICES=''   # GPU 被其他进程占用时用 CPU
.\.venv\Scripts\python.exe tools\eval_retrieval.py
```

数据集：`tests/eval/retrieval_questions.jsonl`。脚本会打印每个阈值下的库内命中率与库外误召回率。

## 知识库浏览

控制台侧栏“知识库”可实时查看已发布文档与 chunk：按 URL/标题搜索、5 秒自动刷新，点击文档右侧显示正文、章节、chunk 序号与抓取时间。

对应 API（需要 `X-API-Key`）：

```powershell
GET /documents?limit=50&offset=0&q=github
GET /documents/{document_id}/chunks?limit=200&offset=0
```

`/documents` 只返回已发布版本；`chunks` 直接从 Milvus 读取，不加载 Embedder。

## 正文提取与质量门禁

提取顺序：Trafilatura → 语义正文区域（`main`/`article`/`role=main`）→ 容器评分 → 浏览器可见文本 → 清理后的 body。导航/侧栏/页脚按链接密度与体量上下文清理，含标题的 header 保留；重复卡片（≥4、同父同签名、带标题）按条目切分，保证名称与描述在同一 chunk。

质量门禁会把无条目结构且链接比例过高的短文本、无标题短导航页、CSS/JS 文本、高重复行判为 `REVIEW_REQUIRED`。

回归脚本：

```powershell
python tools/regression_extract.py
```

输出 Marketplace / AWS / 维普 / 腾讯云 / 术语在线的字符数、chunk 数、正文来源、链接比例与质量门禁结果。

## 故障恢复与清理

- Worker 意外退出：租约过期后由新 Worker 领取，复用校验通过的阶段产物。
- 临时错误：按指数退避重新执行；超过次数进入 DEAD_LETTER。
- 内容失败：进入 REVIEW_REQUIRED，先看 `/tasks?review_only=true` 和 `/events`，再决定重试或新建 Job。
- 修改 Prompt、解析/分块实现或模型：原 Job 的处理指纹会失效，需提交新 Job。
- 新索引未完整可读：不会切换当前版本，旧版本继续提供检索。
- `cleanup` 只删 RETIRED 版本的向量，保留元数据和产物；重复执行安全。
- 中途失去执行权产生的 STAGED 孤立版本不会参与查询，当前需要人工审查后清理，不能直接按年龄删除仍在重试的版本。
- 不要单独清空产物卷；Checkpoint、数据库和索引应成套备份。

## 可选监控

`/metrics` 暴露任务状态数量和已发布文档数；支持 X-API-Key 或 Bearer 认证。
将与 `V2_API_KEY` 相同的值写入 `deploy/api_key.txt`（已 gitignore），在 `.env.v2`
设置 `GRAFANA_ADMIN_PASSWORD`，再运行：

```powershell
docker compose --env-file .env.v2 -f compose.v2.yaml -f compose.monitoring.yaml up -d
```

Prometheus：http://127.0.0.1:9090 ，Grafana：http://127.0.0.1:3000 。
Grafana 已配置 Prometheus 数据源；可用 `scraper_tasks` 和 `scraper_published_documents` 创建面板。
当前交付基础指标与数据源配置，不含性能 Benchmark 或完整运维告警体系。

## 测试

```powershell
python -m pytest -q tests/test_v2_core.py tests/test_v2_fetch.py tests/test_v2_services.py
```

真实集成测试需要可用服务，并在测试专用环境设置：

```powershell
$env:V2_TEST_DATABASE_URL = 'postgresql://user:password@host:5432/test_db'
$env:V2_TEST_MILVUS_URI = 'http://host:19530'
python -m pytest -q tests/test_v2_services.py
```

PostgreSQL 测试创建并删除随机测试 schema；Milvus 测试创建并删除随机测试 collection。
不要使用权限不足或无备份的生产环境运行这些验收。
