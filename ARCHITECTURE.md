# V2 快速交付规格

用户确认：取消 Benchmark；本轮交付代码与交接文档，稍后部署。

## 入口与边界
- V2：`python -m v2`，FastAPI 与中文控制台；V1 CLI/Streamlit 保留用于回退，不与 V2 混写状态。
- 默认单机 Docker Compose，可增加同一宿主机上的 Worker 进程/容器。
- PostgreSQL 保存计划、任务、租约、Checkpoint、文档版本、事件和域名限速状态。
- Milvus 保存不可变版本的 Chunk、Dense 和内置 BM25 索引；RRF 融合后可用 BGE Reranker 排序。
- 原始抓取、解析、提取、验证、Chunk 和 Embedding 均保存至共享产物目录。
- 不开发定时抓取、租户系统、任意公网多用户服务、性能评测和跨机器产物分发。

## 功能契约
- 手动 URL 直接构建 Pydantic CrawlPlan；搜索发现采用 DeepSeek Search 真实结果，LLM 只能选择已返回的网址。
- 发现采用有深度和页数上限的链接/Sitemap 扩展；同一 Job 原子去重、共享页面预算。
- 任务 status 与 stage 分离。领取使用 SKIP LOCKED、租约与 token；失去执行权不能提交阶段或发布。
- 阶段产物先原子落盘，再写 Checkpoint；恢复时检查哈希，损坏后使当前及下游阶段失效。
- 增量跳过要求内容/处理配置不变且当前索引可用。浏览器页面不依靠静态 HTML 的 ETag 跳过。
- 新版本完整写入并可查询后，PostgreSQL 才切换当前版本；查询只检索已发布版本，旧向量延后清理。
- 默认结构解析，保留 LLM 提取和有限修复选项；异常内容进入待审核，基础设施异常重试。
- 内容近似判定为可选 MinHash 候选比较，保留来源关系，不自动合并/删除。
- `/live` 支持知识库优先、未命中获取新证据但不写库；新证据的问答合成仍由调用方决定。
- 基础监控为 PostgreSQL 派生任务状态与已发布文档 Gauge；不是性能 Benchmark。

## 交付验收
本地执行无外部服务的功能测试；外部 PostgreSQL/Milvus 集成测试明确 opt-in。
没有服务和模型的成功执行证据，不宣称已上线或真实端到端验收通过。
