# V2 验证记录

日期：2026-11-12（用户时区 UTC-06:00）。本轮仅交付代码，未部署服务。

## 已执行

最终完整测试命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -q --disable-warnings --junitxml='D:\新建文件夹\workspaces\requirement-web-scrapper-v2\knowledge\pytest-results.xml'
```

结果：**228 passed，4 skipped，52 warnings**。这是功能/回归测试，不是 Benchmark。
其中 V1 205 项通过；V2 23 项通过，4 项外部服务测试跳过。

V2 覆盖：
- URL 规范化、参数保留、去重、域名与私有地址限制。
- 产物哈希、损坏检查、路径越界阻止。
- 索引写入中断后的恢复，避免重复 Fetch/Embedding。
- 产物损坏后的下游重建。
- 过期 Worker 在外部写入后无法发布。
- Chunk 数量减少时使用新版本，旧 Chunk 不属于当前版本。
- HTTP 304 只在当前产物和索引可用时跳过。
- 配置变化要求新 Job；低质量正文保留供审核。
- Planner 拒绝不在真实搜索候选集合中的 URL（测试使用注入候选）。
- 查询时剔除已退役版本；Chroma 过渡适配器版本隔离。
- 真实本地 HTTP 测试站的条件请求、重定向拦截、访问拒绝、正文大小限制。
- API 认证和计划校验。
- 真实 Chromium 中的控制台提交与检索按钮流程（后端响应被测试拦截，不代表真实服务联调）。

另执行：Python 编译检查、CLI `--help`、OpenAPI schema 生成（11 条路径）、
部署 YAML 解析和已安装 PyMilvus 方法签名核对。YAML 解析不等同于 `docker compose config` 或镜像构建。

修复的旧测试问题：`tests/test_app.py` 原本使用相对 `ui/app.py` 路径；
当前 Streamlit 按测试文件目录解析相对路径，因此改为从项目根解析绝对路径。

## 跳过与未执行

`tests/test_v2_services.py` 的 4 项测试：
1. PostgreSQL 任务领取、恢复与 fencing。
2. PostgreSQL 多 Worker 的原子页面预算。
3. PostgreSQL 版本发布与旧任务覆盖防护。
4. Milvus Dense + BM25 的写入、过滤检索与删除。

原因：用户选择稍后部署，当前未设置 V2_TEST_DATABASE_URL / V2_TEST_MILVUS_URI。
未执行 Docker 构建/启动、生产数据库迁移、实际 Tavily 搜索和 V2 全链路模型调用。
以上测试通过前，状态只能是“代码已交付、待服务集成验收”，不能标记“已上线”。

## 本地验证环境中的主要依赖

```text
Python 3.13
psycopg 3.3.9
pymilvus 2.6.18
fastapi 0.119.0
httpx 0.28.1
pytest 9.2.0
playwright 1.65.1
sentence-transformers 5.0.0
chromadb 1.5.9
datasketch 1.10.0
prometheus-client 0.26.0
streamlit 1.67.0
```

这是环境记录，不是可跨平台复用的完整依赖锁文件。

## 2026-11-13 外部服务集成

在本机 WSL Milvus 与独立 PostgreSQL 5433 上执行：

```powershell
$env:V2_TEST_DATABASE_URL = <scraper DSN>
$env:V2_TEST_MILVUS_URI = 'http://127.0.0.1:19530'
.\.venv\Scripts\python.exe -m pytest -q tests/test_v2_services.py
```

结果：**4 passed in 5.28s**。覆盖 PostgreSQL 任务领取/恢复/fencing、多 Worker 原子页面预算、
版本发布与旧任务覆盖防护，以及 Milvus Dense + BM25 的写入、过滤检索与删除。
此前跳过的 4 项外部集成测试已全部执行并通过。完整全量回归仍以 228 passed / 4 skipped 记录为准；
本次未重跑全量。

## 2026-11-13 04:38 全量回归

对齐 V1 的 `browser_visible_text` 调用并新增 `test_parse_passes_browser_visible_text_like_v1` 后：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

结果：**232 passed, 4 skipped, 52 warnings in 100.98s**。总数比 11-12 的 JUnit 记录多 4 项：
3 项 `test_v2_deepseek_search.py`（03:15 新增，未写入旧 JUnit）加本次 1 项。

## 2026-11-13 04:52 质量修复后全量回归

P0-1/P0-2/P1/P2-1/P2-2 及测试更新后：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

结果：**237 passed, 4 skipped, 52 warnings in 94.02s**。

检索评估（`tools/eval_retrieval.py`，8 库内 + 5 库外问题）：

```text
threshold  in_hit   out_evidence   avg_in_top  avg_out_top
     0.00  8/8     5/5            0.948       0.025
     0.05  8/8     1/5            0.948       0.025
     0.10  8/8     0/5            0.948       0.025
     0.20  8/8     0/5            0.948       0.025
```

据此将 `V2_RERANK_MIN_SCORE` 默认值定为 **0.20**（库外最高 0.068，库内最低 0.715）。

## 2026-11-13 05:30 V1 能力恢复后回归

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

结果：**239 passed, 5 skipped, 52 warnings in 85.26s**。

后续新增 EventLogger 降级测试与 UI 重排测试后：**240 passed, 5 skipped**（83.75s）。
新增知识库浏览 API/UI 后：**241 passed, 5 skipped**（83.46s）。
新增通用正文识别/条目切分后：**245 passed, 5 skipped**（85.06s）。
新增代码块围栏测试后：**246 passed, 5 skipped**（86.51s）。

清空数据库后的 7 页面 Job `32151481`：6 篇 COMPLETED、维普 REVIEW_REQUIRED。Marketplace 27 chunk / AWS 20 / 腾讯 4 / 术语 9 / Python asyncio 38（4 个 code fence）/ GitHub README 13。chunk 中无导航标记。

提取回归（`tools/regression_extract.py`）：

```text
[PASS] marketplace: chars=2562 blocks=27 chunks=27 main=True/main link=0.128 items=20
[PASS] aws: chars=9426 blocks=20 chunks=20 main=True/main link=0.083 items=0
[PASS] cqvip: chars=670 blocks=1 chunks=1 main=True/container link=0.179 items=0 review=...
[PASS] tencent: chars=6416 blocks=1 chunks=4 main=True/container link=0.067 items=0
[PASS] term: chars=2874 blocks=10 chunks=10 main=True/container link=0.264 items=0
```
另做 PostgreSQL 中断恢复验证：`/ready` 200、`/query` 正常，Job `d3592464` 恢复完成。

带本地 PostgreSQL/Milvus 的外部集成：

```powershell
$env:V2_TEST_DATABASE_URL = <dsn>
$env:V2_TEST_MILVUS_URI = 'http://127.0.0.1:19530'
python -m pytest -q tests/test_v2_services.py
```

结果：**5 passed in 5.61s**（新增 `test_postgres_cancel_and_delete_url`）。
另做冒烟：浏览器回退 SPA fixture、事件审计、删除 URL、取消 Job，均通过。
日志视图冒烟：控制台表格加载 6 行（含表头），URL 过滤只保留 `example.com`，无 JS 错误。
