# Web-Scrapper V2 开发交接

## 用户要求与本次范围

用户批准 V2 开发，取消 Benchmark，要求快速交付与完整交接。随后明确选择：
**先交付代码与交接文档，稍后部署。** 因此本轮不安装/启动数据库服务，不声称已上线。

源码：`C:\Users\王浩\Desktop\Web-Scrapper-master`。
协作记录：`D:\新建文件夹\workspaces\requirement-web-scrapper-v2`。
原项目没有 `.git`，修改前已归档源文件（不含 `.env`、数据库或虚拟环境）。

## 已实现的代码

| 模块 | 文件 | 说明 |
|---|---|---|
| 配置与契约 | `v2/config.py`, `v2/models.py` | Pydantic 计划，环境变量，处理配置/源码指纹 |
| 持久化 | `v2/schema.sql`, `v2/db.py` | Job、URL Task、租约 token、阶段状态、Checkpoint、版本、事件、域名限速 |
| 产物 | `v2/artifacts.py` | 内容寻址 JSON，临时文件 fsync + 原子替换，读取校验哈希 |
| 采集 | `v2/fetch.py`, `v2/urls.py` | HTTPX、条件请求、有界重定向、Playwright、域名控制、链接/Sitemap |
| 内容 | `v2/content.py` | 复用正文解析、PDF 文本、基础质量分、可选近似重复检测 |
| Worker | `v2/worker.py` | 阶段恢复、有限重试、续租、过期提交防护、发布与手动清理 |
| 存储 | `v2/store.py` | 版本化接口、Milvus Dense/BM25/RRF、过渡 Chroma 适配器 |
| 查询 | `v2/retrieval.py` | 当前版本过滤、Reranker、带来源的证据、可选 LLM 回答 |
| Planner | `v2/planner.py`, `agents/prompts/plan.txt` | DeepSeek Search 搜索结果约束的 LLM 选源 |
| API/UI | `v2/api.py`, `v2/index.html` | 手动采集、自主计划、问答、任务/审核、重试、事件、健康与指标 |
| CLI | `v2/__main__.py` | 初始化、提交、运行 Worker、查询、重试、清理、V1 URL 再采集迁移 |
| 部署 | `Dockerfile.v2`, `compose*.yaml`, `deploy/` | 单机部署及可选 Prometheus/Grafana 数据源配置 |

默认选择：**BGE-M3 Dense + Milvus BM25 + RRF + BGE Reranker**。
BM25 与 BGE-M3 Sparse 没有混用。Reranker 可以显式禁用，但不存在静默模型替换。
默认提取为确定性正文结构解析，可通过计划显式开启原有 LLM 提取与修复。

## 核心不变量

1. 同一 Job 的 URL Fingerprint 唯一；不同 Job 可以刷新同一 URL。
2. Task 的 status 与 stage 分离；RUNNING 写入均要求有效 lease_token 和未过期租约。
3. 每个阶段先保存可校验产物，再写 Checkpoint。任一产物损坏使当前及后续阶段失效。
4. 版本 ID 包含 task、处理指纹和 Chunk manifest 哈希；不同执行产物不会覆盖其他版本。
5. Milvus 写完且可查询后才发布 PostgreSQL current_version；查询只允许当前已发布版本。
6. 较早提交 Job 的慢结果不能覆盖较晚 Job 的已发布版本。
7. 清理只处理 RETIRED；未发布 STAGED 版本不可见，保留供恢复/人工排查。
8. 内容未变不等于可以跳过：处理指纹、产物和当前索引也须可用。

## 测试与证据

详见 `V2_TEST_RESULTS.md`。本地无外部服务测试与 V1 回归均已执行。
真实 PostgreSQL/Milvus 集成、真实搜索/LLM 和完整模型链路仍需部署后验证；
不能把 Fake/Mock 测试等同于服务集成通过。

## 下一位接手者首先做什么

1. 阅读 `AGENTS.md` 顶部 V2 覆盖规则、`SPEC_V2.md`、本文件和 `V2_QUICKSTART.md`。
2. 确定部署机器与资源，配置环境变量，不把密钥写入交接文档或版本库。
3. 构建 Compose，检查服务健康，再运行 4 项 opt-in 集成测试。
4. 用一个公开静态页面完成采集→入库→查询，再验证 JS 页面和 LLM 提取选项。
5. 同一页面重复提交新 Job，检查 UNCHANGED；修改测试站内容，验证新版本发布。
6. 运行中停止一个 Worker，等待租约过期，确认新 Worker 从 Checkpoint 恢复。
7. 单 Worker 验收通过后再增加 Worker。不要同时更换模型、改变配置和扩大并发。

## 尚未验证与已知边界

- **未部署**：未执行 Docker build/up、真实 PostgreSQL/Milvus 测试或在线模型联调。
- **单机共享产物**：跨机器 Worker 必须先接入共享文件系统/对象存储。
- **页面预算**：max_pages 限制每个 Job 的唯一页面任务数；不包含请求重试、Sitemap 与浏览器资源请求。Sitemap 另外有请求次数上限。
- **V1 保留独立**：旧 Streamlit 仍查看 SQLite/Chroma，不展示 V2 数据；其旧并发入口的问题没有在本次重写。
- **迁移是再采集**：`import-v1` 读取已完成 URL；不导入旧 Chunk 快照或旧向量，历史离线页面需单独迁移。
- **近似去重是轻量实现**：可选 MinHash 比较最近 100 个当前文档，不含全库 LSH 服务；命中进入审核，不自动删除来源。
- **质量分是启发式**：长度与行重复度，不是事实可信度评估或完善的 PII 脱敏系统。
- **BM25 默认标准分析器**：中英混合、中文分词和领域词典没有调优；需明确改变 schema/处理版本再重建。
- **查询活动版本集合**：当前从 PostgreSQL 读取对应 collection 的全部活动版本构建过滤条件；不是面向百万文档的最终实现。
- **孤立 STAGED 与产物保留**：有记录可追溯，但自动回收策略尚未实现，需避免长期无界增长。
- **404/410**：当前作为抓取错误有限重试，不自动删除曾发布的知识；删除/过期策略需按业务另定。
- **URL 与访问策略**：默认拒绝私有地址并逐跳检查域名；跨域 CDN 需要加入 allowed_domains。
  DNS 预校验不替代网络层出站隔离，不能把本版本直接作为不可信公众的任意 URL 代理。
- **实时查询**：V2 `/live` 未命中返回新抓取证据，不写库，也不自动生成该新证据的 LLM 回答。
- **监控**：交付基础 Gauge 与 Grafana 数据源，没有完整仪表盘、告警或 Benchmark。
- **依赖**：采用版本范围，未生成跨平台完整锁文件；部署联调后应记录实际镜像与依赖版本。

## 回退与数据保护

V1 `.env`、`data/`、SQLite/Chroma 未被修改或复制到交付包。V2 使用独立表前缀、collection 和产物目录。
回退只需停止 V2 并使用 V1；不要删除 Docker 数据卷或旧库来“清理失败任务”。
源码快照位于协作工作区 `archive/`。新增源文件为主，原文件变更包括入口文档、规则、
`.gitignore` 和旧 Streamlit 测试的绝对路径修正。

## DeepSeek 统一接入 — 2026-11-13T03:16:44-06:00
模型已有配置为 deepseek-v4-pro，读取本地 DEEPSEEK_API_KEY。自主发现已从 Tavily 改为 DeepSeek 官方 /v1/tools/search，使用同一环境变量；不向第三方传递该密钥。超过 128 字符的目标先由模型生成短搜索词，选源仍限定真实搜索候选。20 项相关功能测试通过；真实搜索与模型选源成功生成 Python asyncio 采集计划。API 已重启并健康检查 200。Milvus 与 Worker 仍未启动，用户选择保持 Milvus，不改 Chroma。

## 本地端到端 — 2026-11-13T03:58-06:00
本机没有 Docker Desktop，已用独立 WSL 发行版 `milvus` 提供 Docker Engine 与 Milvus 2.6.0；MinIO 为该发行版内的原生进程，etcd/Milvus 采用 host 网络。Windows 经 localhost 访问 19530。环境说明与重建步骤见协作工作区 `knowledge/local-milvus.md`，启动脚本为 `knowledge/start-local-milvus.ps1`。

- `tests/test_v2_services.py` 4 passed：PostgreSQL 领取/恢复/fencing、原子页面预算、版本发布防护、Milvus Dense+BM25 回环。
- 发现策略的边界：`links+sitemap` 会把导航页扩到 `max_pages`，并可能在一个大页面的 EMBED 阶段长时间占用；本次最终改用 7 个种子、`discovery_strategy=seeds`、`max_depth=0` 的 Job 完成验收。
- 7 个种子的结果：3 条新发布（维普文献页、GitHub ArcLibrary README、AWS 博客）+ 2 条 UNCHANGED（腾讯云、术语在线，复用已发布版本），共 5 个已发布文档；chinaxiv 返回 403 → BLOCKED；cadal 返回 404（单斜杠变体仍 404）→ DEAD_LETTER。
- 本机代理把 github.com / aws.amazon.com 解析到 198.18.0.0/15（fake-IP），默认私有地址校验会拒绝。只有显式设置 `V2_ALLOW_PRIVATE_NETWORK=true` 才能抓取；这是本机网络环境的例外，不要写进正式部署默认值。
- 查询：GPU（RTX 3060 12GB）被其他进程占用约 10.7GB 时，API 以 `CUDA_VISIBLE_DEVICES=""` 在 CPU 运行，冷查询约 62s、热查询约 8s；reranker 从 `data/v2/models/bge-reranker-v2-m3` 本地目录加载，避免 Windows 无符号链接权限导致的 HF 缓存失败。
- 尚未做正式 Compose 部署；WSL 内 dockerd、MinIO、Milvus 不会开机自启，主机重启后需先运行启动脚本。

## 已修复：CSS/JS 污染与检索阈值 — 2026-11-13T04:52-06:00
原缺陷：`pipeline/strip.py` 的兜底 `lxml.text_content()` 会把 `<style>/<script>` 文本当正文，AWS/维普等 HTTP 抓取的 JS/CSS 重页面入库大量 CSS/JS chunk。该逻辑继承自 V1；V1 的 `test_script_only_html_falls_back` 曾断言脚本内容必须保留。

本轮按优先级修复：

- **P0-1**：兜底前删除 `script/style/noscript/template/svg/iframe/canvas/object/embed/nav/footer/aside/form`；新增 `looks_like_css_js()`。
- **P0-2**：`v2/content.py::quality()` 命中 CSS/JS 特征即 `ReviewRequired`，异常文本不入库。
- **P1**：`strip_html` 在 Trafilatura 无输出、占比过低或疑似 CSS/JS 时优先使用 `browser_visible_text`；V2 的 browser 结果携带 `visible_text` 并由 `parse()` 传入。
- **P2-1**：`Settings.rerank_min_score` 默认 **0.20**，rerank 后过滤弱证据；全部过滤时返回 `No sufficiently relevant evidence found`。
- **P2-2**：新增 `tests/eval/retrieval_questions.jsonl` 与 `tools/eval_retrieval.py`。评估结果：库内 8/8 命中（最低 0.715），库外最高 0.068；0.05 时仍有 1/5 误召回，0.10 起 0/5，选定 0.20。

重采与验证：新 Job `2d32bad1-9796-4f9e-a909-a636acf6ba29` 重采 5 篇，全部 COMPLETED；当前 chunk 数为 AWS 6、腾讯 4、GitHub 3、维普 1、术语在线 3。`/query`「查理九世是谁写的？」返回 0 条证据；「ArcLibrary 是什么项目？」返回 1 条、0.935、正文无花括号。全量 `pytest` 237 passed / 4 skipped。

遗留：维普那篇静态页只有导航（正文需登录/付费），仍是 1 个低价值 chunk；需要换源或显式 `--extract llm`。V1 与 V2 共用 `pipeline/strip.py`，本次修复同时改善 V1 的正文清洗。

## V1 能力恢复 — 2026-11-13T05:30-06:00
按用户要求，把 V1 完全缺失/弱化的能力补回，不删除 V2 能力。新增/改造：

- `v2/obstacles.py`：V1 障碍策略（cookie/弹窗/CAPTCHA iframe/auth/DOM drift/stale/blocked clicks/慢响应/429/5xx/load more/重定向）；
- `v2/browser.py`：Worker 级共享 Chromium；
- `v2/site_config.py`：复用 `config/fetch.json`、`obstacles.json`、`orchestrator.json`、`embeddings.json`（Schema 校验）；
- `v2/fetch.py`：UA/Accept-Language、编码回退、逐请求 429/5xx 重试、`looks_empty()` 静态→浏览器判断、settle+滚动、翻页；
- `v2/worker.py`：共享浏览器、取消检查、结构化结果 deterministic validator、fetch/strip/validate/chunk/publish 事件；
- `v2/db.py` + `schema.sql`：`cancel_job`、`job_cancelled`、`delete_url`、`delete_tasks_for_urls`、`clear_events`、`cancelled_at`；
- `v2/api.py`：`DELETE /documents`、`POST /jobs/{id}/cancel`、`DELETE /events`、`/live` 可生成回答、`/query` 写 answer 事件；
- `v2/__main__.py`：`fetch/strip/extract/chunk/live/delete/events/cancel/run`；
- `v2/index.html`：任务进度自动刷新、停止、按 URL 实时问答、删除 URL、事件日志/清空、回答与引用渲染；
- 日志视图：可读表格（时间/事件/URL/结果/详情），支持 URL 片段、事件类型、条数过滤与 2 秒自动刷新；`/events` 支持 `limit/event_type/url/task_id`；
- `v2/config.py` + `v2/runtime.py`：嵌入模型显式回退（默认开启并写事件）。

验证：全量 **239 passed / 5 skipped**；外部集成 **5/5 passed**（新增 cancel + delete URL）。浏览器回退 fixture 成功（`fetcher: browser`，visible 420 字符）；事件审计包含 fetch/strip/validation/chunk/publish；删除与取消冒烟通过。

剩余差异（详见 `knowledge/V1_V2_GAP.md`）：默认提取仍是结构化（LLM 可显式选择）、`--mode` 仅兼容参数、V1 独立模块 CLI 改为 `v2` 子命令、V1 Streamlit 改为单页控制台、两套存储不互通。

## 控制台 UI 重构 — 2026-11-13T05:45-06:00
`v2/index.html` 从 9 个纵向堆叠 section 改为侧栏 5 视图（采集/问答/任务/日志/数据）：顶部访问密钥与服务状态，采集/问答双卡片，任务进度自动刷新与停止，日志可读表格（URL/事件类型/条数过滤、2 秒自动刷新、详情折叠），数据页语料指标与删除危险区。移动端 820px 横向导航正常。截图：`knowledge/v2-console-redesign.png`。

## 运行稳定性 — 2026-11-13T06:05-06:00
本机 PostgreSQL 曾因从会被回收的后台 job 中 `pg_ctl start` 启动而被连带杀掉，表现为 `/query` 返回 503、Worker 因 `ConnectionTimeout` 退出。已改为 `Start-Process postgres.exe` 脱离进程树，新增 `start-local-postgres.ps1` / `start-local-services.ps1`；事件写入改为 best-effort，Worker 循环捕获数据库异常并 5 秒重试，不再退出；API 503 提示改为检查 `/ready`。全量测试 240 passed / 5 skipped。

## 知识库浏览 — 2026-11-13T07:40-06:00
新增“知识库”控制台视图与 `GET /documents`、`GET /documents/{id}/chunks`：按发布时间列出已发布文档（标题/URL/chunk 数），点击文档直接读取该版本 Milvus chunk（按 `chunk_index` 排序），支持 URL/标题搜索与 5 秒自动刷新。`v2_documents` 新增 `page_title`，Worker 入库时写入；旧文档标题为空时回退 URL。全量测试 241 passed / 5 skipped。

## 通用正文识别与条目切分 — 2026-11-13T08:20-06:00
新增 `pipeline/main_extract.py`：语义 `main/article/role/itemprop` 优先，容器评分兜底；上下文清理 header/nav/aside/footer（含标题的 header 保留，导航按链接密度与体量判断），不做无条件删除；同父同签名的重复卡片（≥4、长度相近且带标题）识别为条目，每个条目独立 section，短标题块把名称写回正文。`strip_html` 顺序为 Trafilatura → 结构化正文 → 浏览器可见文本 → 清理后的 body；条目结构优先于 Trafilatura 的通用块。

`v2/content.py` 质量门禁新增导航噪声/无标题短页检测，命中进 `REVIEW_REQUIRED`。回归样本：Marketplace 3→27 chunk、无导航、Zube 名称+描述同 chunk；AWS 20 chunk；腾讯 4 chunk；术语在线 10 chunk；维普转为 REVIEW。`tools/regression_extract.py` 可重复执行；全量测试 245 passed / 5 skipped。

## 清空数据与 7 页面回归 — 2026-11-13T08:35-06:00
用户要求先清空已有内容：TRUNCATE 全部 V2 表、删除 Milvus `scraper_v2_*` collection、清空 `data/v2/artifacts`，保留 schema/配置/服务。随后用 7 个页面跑 Job `32151481`：Marketplace 27 chunk、AWS 20、腾讯 4、术语 9、Python asyncio 38（含 4 个保留缩进的 fenced code chunk）、GitHub README 13；维普 `REVIEW_REQUIRED` 未入库。chunk 中无导航标记。全量测试 246 passed / 5 skipped。

## 控制台界面重做 — 2026-11-13T21:50-06:00
侧栏改为顶部 6 个 tab，删除全部 `<small>`/说明性小字/步骤/hint；内容居中（1120px），统一圆角、边框、阴影和深色胶囊 tab；采集参数 2×2；390px 无横向溢出。功能 ID 与交互不变，`tests/test_v2_ui.py` 通过，Playwright 检查 0 JS 错误。截图见 `v2-console-redesign.png` / `v2-console-kb.png`。
