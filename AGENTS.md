# AGENTS.md — 智能计价系统·大模型智能体服务

本文件是本项目所有开发会话的知识基线。改动架构、契约、公式前先读本文件；做出新决策后必须回写本文件。

## 1. 项目使命与边界

为军用软件计价系统（前后端已建成，后端代号 pa-pricing-service，Java）开发**独立的大模型智能体服务**（本仓库 `src/`，Python 3.12+ / FastAPI）。服务职责：

- 接收后端异步任务（文档 URL + 业务上下文），完成**识别与判断**（估算对象、功能需求、功能点类型与计数、审价合理性）；
- 完成**测算计算**（功能规模、综合费用、成本明细）——注意：职责已从"后端计算"（旧 4.9 文档）变更为"智能体服务计算"（0921 新文档），后端只做测算概览汇总；
- 支持多轮对话（参数调整类改动需**同步重算全套表格并返回**）。

**不做**：测算概览汇总（后端做）、目标价封顶与告警（后端概览层做）、前端适配（另有团队）。

## 2. 接口契约（依据《智能计价-大模型任务接口文档0921.docx》+ 双方决议）

### 2.1 端点

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/v1/llm/tasks` | POST | 提交分析任务，请求体 LlmRequest；立即返回 `{calculationId, status:"PROCESSING"}` |
| `/api/v1/llm/tasks/query` | POST | 查询任务，请求体 `{calculationId}`；返回 `{calculationId, status, result, errorMsg}` |

- 任务状态仅 3 值：`PROCESSING / SUCCESS / FAILED`（无 QUEUED/进度字段，后端已放弃进度上报）。
- 任务不存在/已过期：`status=null` + `errorMsg="任务不存在或已过期"`（HTTP 仍 200）。
- 鉴权：静态 token，请求头 `X-Api-Token`（env `AGENT_API_TOKEN`，为空则不校验，仅限开发）。
- **文件获取**：url 支持 http(s)://（生产 MinIO 预签名）**或本地路径**（联调期代替 MinIO，fetcher 自动识别）。
- 幂等（2026-09-28 完善重试语义）：同一 `calculationId` 重复提交——PROCESSING 中→回 PROCESSING 不重复执行；
  SUCCESS 后→回 SUCCESS 可取原结果；**FAILED 后→视为重试重新入队执行**（契约"失败由后端重提"落地）；
  载荷不一致记 warning 不拒绝。
- 任务终态保留 24h（TTL），过期即"任务不存在"。
- 参数校验失败：HTTP 400 + `{errorMsg}`。
- 服务重启：遗留 PROCESSING 任务置 FAILED（errorMsg 注明可重试），由后端重提。

### 2.2 请求（LlmRequest）关键字段

- `calculationId`：调用方生成，任务唯一标识，两侧合一，必填。
- `type`：`analysis`（触发测算）| `conversation`（普通对话）。
- `toolCode`（含义已确认）：

| code | 工具 | 阶段 |
|---|---|---|
| COST_ESTIMATION | 经费估算工具 | 竞标决策 |
| NO4_QUOTATION | 四号文报价工具 | 投标 |
| COST_MEASUREMENT | 真实成本测算工具 | 全要素策划 |
| NO4_AUDIT | 四号文审价工具 | 外协谈价 |
| NO4_QUOTATION_REVIEW | 四号文报价工具（可信四） | 需求评审 |

- `targetMaxPrice`：目标价上限，仅 NO4_QUOTATION 使用（旧文档"仅审价"已作废）。
- `files[]`：`fileName / fileType / category / url(预签名)`。category 枚举（FileUploadTypeEnum）：`TECH_REQ / TECH_PROPOSAL / DETAILED_TECH_REQ / OVERALL_TECH_PLAN / OUTSOURCE_TASK / OUTSOURCE_QUOTE / REQ_SPEC`。
- `chatHistory[]`：`role(user/assistant) / content`。
- 历史上下文（首轮为空，对话时不为空）：`originalFpList / fpScaleList / costItemList / costDetailList / auditPriceList`。

### 2.3 响应 result（LlmResponse）

`content / originalFpList / fpScaleList / costItemList / auditPriceList / costDetailList`。
各元素字段以文档 3.4.1–3.4.5 定义为准；`auditPriceList` 仅 NO4_AUDIT，`costDetailList` 仅 COST_MEASUREMENT。

### 2.4 已裁决的契约问题（勿再讨论）

| 问题 | 结论 |
|---|---|
| 定义与示例冲突（如 category 示例 TECH_REQUIREMENT、costItemList 示例含 moduleSystem） | **以定义为准** |
| `auditPriceList` 字段名 | `moduleConfig`（须与 originalFpList 一致） |
| 金额单位 | **接口全部用元**；内部按公式以万元计算，输出时 ×10000（见 §4） |
| 业务默认值 | **按原型默认**：regionRate=2.8、allocationFee=0、talentSalary=0、reuseFactor=1.0、F1–F7=0 |
| 对话修改功能点 | 我方同步重算 fpScaleList/costItemList/(costDetailList) 全量返回，后端全量替换 |
| 对话返回粒度 | 全量列表，不做增量 patch |
| 审价理由字段 | 暂不加 auditReason |
| costDetailList（文档 3.4.6 自认有问题） | 按我方修订稿实现（moduleSystem/moduleConfig/softwareObject/requirementName/cost/outsourceUnitCost/outsourceUnit），待后端确认 |
| 输入文件格式 | **只支持 docx**（2026-09-28 决策）：.doc 等由前端引导用户手动转换后重传；服务端拒绝非 docx 文档类输入并返回明确 errorMsg；镜像不含 LibreOffice |
| **首轮对话触发测算**（2026-09-28 后端抓包确认） | 后端真实用法：首轮 type=conversation 携带 files 且无历史功能点（用户说"请基于上传的文件测算"）→ 自动路由到分析管线；带历史列表的 conversation 才是对话分支 |
| **fpCount 口径**（2026-09-28 业务定标，推翻 0921 示例的 fpCount:1） | fpCount = **原始功能点数 = 标准权重×个数**（个数默认 1）：四号文系 ILF 7/EIF 5/EI 4/EO 5/EQ 4，经费估算 ILF 35/EIF 15；模型只判条目与类型不出数字，折算在 validator；估算对象原始功能点 = **Σ fpCount 简单求和**（assembler） |

## 3. 架构（已定稿，勿轻易引入新框架）

```
后端（calculationId、业务状态、概览汇总）
  → FastAPI 服务（单进程单机；不存业务状态）
      ├─ API 层        提交/查询；静态 token
      ├─ 任务管理器    asyncio worker 池（默认4）；SQLite(WAL) 存任务运行态；幂等；TTL 24h
      ├─ 编排层        analysis / conversation 两条流水线；按 toolCode 路由
      ├─ 识别管线 S0–S5（见 §5）
      ├─ 公式引擎      全部确定性计算在代码（见 §4），模型永不产出金额
      ├─ LLM 抽象层    LiteLLM；env 切换 mock / 在线 API / vLLM / Ollama
      └─ skills/       判定知识资产（见 §6）
```

**已否决的技术选型（及理由，勿翻案）**：LangChain（依赖重、抽象泄漏，本任务是确定性流水线）；LangGraph（现只有线性流程，二期出现多功能循环再评估）；Celery/消息队列（单机几十用户，过度设计）；Milvus/独立向量服务（单机养不起）；**OpenCode/Claude Code 内嵌**（编程智能体任务错配、shell 执行过不了军工安全评审、Claude Code 绑定 Anthropic 无法内网部署）。

**LLM 接入**：LiteLLM 统一客户端，`LLM_BACKEND=mock|litellm`；交付环境为私有化 vLLM/Ollama（GLM/Qwen/DeepSeek），开发期可用在线 API。结构化输出 = JSON mode + Pydantic 校验失败回喂重试（≤2 次）。

## 4. 公式引擎规格（依据《智能计价工具-数据计算公式汇总 v3.0》）

**单位规则**：费率类字段（regionRate 2.8）按公式文档口径（万元/人月）；金额输出字段全部为元 = 万元值 ×10000。

- 权重表（fpCount=**原始功能点数=标准权重×个数**，个数默认 1，模型不出数字、折算在 validator；originalFp=ΣfpCount 简单求和）：
  - COST_ESTIMATION：`UFP = 35×NILF + 15×NEIF`（仅 ILF/EIF 两类）
  - 其余四个工具：`UFP = 7×NILF + 5×NEIF + 4×NEI + 5×NEO + 4×NEQ`
- 复用因子 reuseFactor ∈ [0.5, 1.0]，默认 1.0
- 调整因子 = `1.0 + 0.1×(F1+…+F7)`，F 取值 0/1/2，默认全 0
- 调整后功能点 = 原始功能点 × 复用因子 × 调整因子
- 工作量（人月）=（调整后功能点 × 耗时率）÷ 176；耗时率按**原始功能点数**分档：≤1000→8.46；1000–2000→6.08；2000–5000→5.27；5000–10000→5.14；>10000→5.09
- 测算金额（万元）= 工作量 ×（regionRate − allocationFee）；小计 = 测算金额 + talentSalary
- 外协成本 =（外协单位在某分系统下功能点 ÷ 分系统功能点）× 该分系统全自研总成本；outsourceUnit 为空 = 自研
- 粒度：originalFpList 按需求条目；fpScaleList 按 softwareObject；costItemList 按 moduleConfig；costDetailList 按需求条目（含外协归属）
- 验算基准（示例自洽）：1 个 ILF（NO4_QUOTATION）→ originalFp=7 → workload=7×8.46/176=0.3366 → 0.3366×2.85=0.9594 万元 ≈ 9594 元

## 5. 识别管线 S0–S5

```
S0 格式归一化   内容嗅探（本仓库资料中出现过 HTML 伪装的 .doc！）；★只支持 docx（2026-09-28 决策）——
                .doc 等旧格式由前端引导用户转换，服务端拒绝并返回"另存为 .docx 后重传"；
                xlsx 供审价报价文件（openpyxl）；pdf 解析器暂未接入（装 pdfplumber 后启用）。
                （历史上曾实现 soffice→wordconv→WordCOM 三级转换链并实测 936 页巨型 .doc，
                 docx-only 决策后已删除；wordconv 可绕过 Word OFV 拦截的知识保留在此备查）
S1 Markdown IR  docx/xlsx/pdf → Markdown 中间表示（markitdown 优先，python-docx 兜底）
                ★实弹验证后已适配：军工模板自定义样式（"章标题"→L1、"N级有标题条"→L(N+1)，须含"标题"字样）；
                标题尾部页码清洗（_strip_page_ref）；★列表项防误判三件套（建设方案实测）：
                ①"."/";"开头结尾的自动编号残留不判标题 ②无标题样式时单级编号"3. xxx"判为列表项非章节
                ③编号剥离后清理残留分隔符（"3. xxx"→"xxx"）
                ★两个已知坑仍在防护位：自动编号回填（numbering.xml）、合并单元格旁路（已检测）
                章节树 = 行级解析（标题样式 + 编号正则 ^\d+(\.\d+)* / ^[一二三四五]、/ ^第X章）
S2 相关章节定位 规则粗筛（功能/性能/接口/数据/模块关键词）+ 目录级轻量分类
                （实测 8 篇真实需规保留率 89~94%、936页建设方案 82%，规则偏保守宁多勿漏，符合穷举原则）
S3 估算对象切分 章节树→三级映射（v2）：moduleSystem=项目名（请求 projectName）；
                分组章节过滤（需求/能力需求（FR）/总体设计/xx架构 等分类容器不作层级）；
                moduleConfig=有效层级第一层（FR_1/子系统）；softwareObject=有效层级最后一层（叶子功能块）；
                已验证 GJB-438B（FR 树）与建设方案（子系统树）两种结构
S4 功能点抽取   按配置项分段并行（asyncio.gather）；LLM 只判类型与条目，计数与权重折算在代码
S5 校验归并     Pydantic 校验回喂重试；跨段去重；文档自带汇总表则交叉对账
```

解析结果按 `文件URL+ETag` 缓存；对话轮次免重复下载解析。

## 6. Skills（判定知识资产）

以工作区根《功能点报价SKILL.md》（五元素 26 条核心规则 + R-ENHANCE 增强识别 + 报价套路 + 自检清单）为知识基线，`src/app/skills/` 按目录打包：

| skill | 内容 | 绑定工具 |
|---|---|---|
| hierarchy_mapping | 三级结构归属 + R-MASTER-1 顶层软件识别 + 表格驱动兜底 | 全工具（前置） |
| fp_pricing | **核心**：26条规则+增强识别+R10/R11报价套路+自检清单 | NO4_QUOTATION / NO4_QUOTATION_REVIEW / COST_MEASUREMENT |
| cost_estimation | 经费估算变体：仅 ILF/EIF（35/15 口径），竞标穷举 | COST_ESTIMATION |
| no4_audit | 审价：A1~A6 合理性规则（映射报价规则为标尺）+ 费用阈值 | NO4_AUDIT |
| cost_measurement | 外协归属判定（O1~O4：可外协特征/自研保留/判定来源/对象粒度） | COST_MEASUREMENT 追加 |
| dialog_intents | 对话意图分类 + 引用 R 规则编号/公式作答 | type=conversation |

加载策略：**按 toolCode 静态绑定组合**（`extractor._SKILL_COMBOS`），不做运行时动态选择。原则：判定知识进 skill，计算规则进代码；skill 改动必须过评测回归。输出契约：`description` 必须含"章节溯源+规则依据"（R26）；审价链路模型输出 `auditResult`（1/0），管线原样透传，缺失时 mock 确定性补齐。（旧 fp_classification/requirement_extraction/audit_rules 已并入上述结构删除）

## 7. 评测与质量

- **实弹测试集**（已全部为 docx）：`测试文件/01HNSF02_需求规格说明（公开）/`（8 篇真实需规，
  25~96 页，WPS/Word 混合来源；对应工具⑤ NO4_QUOTATION_REVIEW）+
  `测试文件/设计方案/海南商发C3I二期建设方案.docx`（936 页巨型总体方案；对应工具①②③）。
  探针：`python scripts/probe_docs.py "../测试文件" [--tree 关键词]`，
  真实抽取：`python scripts/extract_real.py <关键词> --tool TOOLCODE --filter <章节正则> [--limit N]`。
- 金标准：工作区根目录 5 个公开模板（1经费估算/2四号文报价/3真实成本/4四号文审价doc/5需求评审报价），其表格即期望输出。
- 指标按阶段：S2 章节定位召回、S4 条目抽取 P/R、fpType 分类准确率、fpCount 对账误差。
- 每次改 skill/公式/参数跑回归（pytest + probe 双轨）。

## 8. 目录结构

```
D:\WORK\智能计价\
├─ AGENTS.md            本文件
├─ 原型\                5 个工具前端原型（chat 面板均为 mock，接入断点：无采纳按钮）
├─ *模板*.xlsx/.doc     金标准评测素材
├─ *接口设计文档*.doc    旧 4.9 契约（已被 0921 版取代，仅存档）
├─ 智能计价-大模型任务接口文档0921.docx   ★现行契约
├─ 智能计价工具-数据计算公式汇总.doc      公式依据 v3.0
└─ src\                 智能体服务工程（容器化交付：Dockerfile + docker-compose.yml）
   ├─ app\{api,core,schemas,tasks,pipeline,engine,agents,llm,skills}
   ├─ tests\
   └─ requirements.txt
```

## 9. 开发约定

- **交付形态：容器化**。`src/Dockerfile`（python:3.12-slim，非 root 用户 `agent` 运行，内置全部可选依赖，HEALTHCHECK 走 `/api/v1/llm/health`）+ `src/docker-compose.yml`（任务库走**命名卷** `pricing-agent-task-data`（首挂继承镜像属主 agent:1000，无宿主机权限坑；卷名跨版本固定升级不丢数据））。**离线交付用 `bash scripts/package_docker.sh [版本]`**：构建→save→生产 compose（剥离 DEV-ONLY 测试挂载与 build 段）+生产 .env 模板+部署说明+SHA256→`dist/pricing-agent-<版本>-<日期>.tar.gz`（实测 158MB，load/校验/compose 均验证通过）。
- 依赖最小化（军工内网离线部署 + 安全评审）：开发环境核心仅 fastapi/uvicorn/pydantic/httpx；litellm、python-docx、openpyxl、pdfplumber 为镜像内置可选项，import 必须可降级。**发布版需 `pip freeze` 锁定版本再构建**（安全评审要求可复现）。
- 本机开发 Python：**conda 专用环境 `pricing-agent`**（Python 3.12，与镜像版本对齐；勿用 base）。路径 `C:\Users\ningh\.conda\envs\pricing-agent\python.exe`，激活：`conda activate pricing-agent`。核心+可选依赖（litellm/python-docx/openpyxl/pdfplumber/pytest-asyncio）已全部安装。注意：创建环境须用 `-c conda-forge --override-channels`（默认 Anaconda 源有 ToS/商用授权限制）。
- 敏感性：解析内容内存处理不留存；任务库只存结果 JSON 与状态，不存原文；日志不落正文内容。
- 测试：契约/schema、公式引擎、任务管理器必须单测；LLM 相关走 mock 回归。
- 提交响应恒 200（PROCESSING），错误一律走 errorMsg 字段（FAILED）或 HTTP 400（参数错）。

## 10. 内网适配原则（2026-09-28 定标，长期遵守）

**网络边界：开发机（外网）负责构建；服务器（内网）只运行，零外网依赖。**

- 运行期禁止任何外网访问：litellm 关遥测（`litellm.telemetry=False`，client.py）+
  `LITELLM_LOCAL_MODEL_COST_MAP=True`（镜像 ENV + .env 模板，跳过 raw.githubusercontent.com 价格表拉取——
  服务器无外网时该拉取重试 3 次每次拖慢约 10s）；
- 构建期适配国内网络：Dockerfile pip 用清华镜像源；
- 新增依赖时审查其运行期网络行为（遥测/版本检查/资源下载一律关闭）；
- 交付走 docker save/load（package_docker.sh），服务器不需要镜像仓库和外网；
- 服务器可访问的内网服务：模型网关（LLM_BASE_URL）、MinIO（文件 URL）。

## 10. 待办与开放风险

- [x] ~~接口输入输出全链路实测~~（2026-09-28，LLM_BACKEND=litellm + 本地路径代替 MinIO）：
  analysis 提交→轮询 SUCCESS（二期05：173 条功能点、112.38 万元、金额元单位 ✓）；
  conversation 重算（传入列表→全套表格返回 ✓）；FAILED（文件不存在→明确 errorMsg ✓）；
  任务不存在（status=null ✓）；幂等重提交（返回终态不重复执行 ✓）。20 单测全过。
- [x] ~~conversation 参数编辑落地~~（2026-09-28 修复）：`pipeline/editor.py` 结构化编辑应用层——
  模型按 dialog_intents skill 契约输出 `{intent, reply, edits[]}`（edits 为 set/remove/add 操作，find 按
  requirementName+可选范围定位），代码确定性应用→重算→回复附【已应用】/【未应用】摘要，保证叙述与数据一致；
  计算结果字段（workload/金额）不可直接 set，只能改功能点间接重算。
  **歧义防护（实测教训×2，最终解法）**：名称匹配以**用户原话为最高裁决信号**——
  ① 模型缩写（用户说"导出需求可追踪报告"，find 给"导出需求"且恰好存在同名短条目）→ 用户原话含全名时
  重定向到全名，避免删错；② 精确命中（"用户信息"）不被包含它的更长名称（"查询用户信息"）误判为歧义
  （第一版防护过度导致正常编辑被拒）；③ 模糊命中多个名称时取用户原话中最长（最具体）者，无线索才拒绝并列候选。
  apply_edits(user_message=...) 传入最后一轮原话。edits 缺失纠正重试：模型偶发只在 reply 叙述修改——
  检测到修改叙述但 edits 为空时带纠错指令重问一次；system prompt 加硬约束；编辑应用数记入日志。
  结构化输出失败自动降级纯文本对话（不中断）。35 单测全过。
- [x] 接口冒烟脚本 `scripts/api_smoke.py`：16 项断言（analysis 全链路/对话编辑落地/FAILED/不存在/幂等/400/健康检查），
  mock 与 litellm 两种模式实测全过（Docker 连续两遍 16/16）；`--mock` 跳过仅真实模式适用的用例；失败退出码 1。 **`--body-file req.json` 粘请求体直测模式**：支持原始 LlmRequest 或后端抓包格式 `{body,headers}`（自动解包），提交→轮询→响应存档→摘要。
  **两个环境坑（实测踩过）**：① Docker 跑服务时本地路径要用容器内挂载路径——compose 已挂载
  `../测试文件:/data/testfiles:ro`，Git Bash 下用 `--docker` 参数（代码内拼接路径，规避 MSYS 把
  `/data/...` 转成 `D:/Program Files/Git/data/...`）；② 改代码后必须 `docker compose up -d --build`
  重建，旧容器跑旧代码（旧 fetcher 报 httpx 协议错即是此因）。生产环境移除测试语料挂载，文件走 MinIO。
- [x] ~~S3 层级映射优化~~（2026-09-28）：moduleSystem 统一为项目名（API 实测 160/160 条 ✓）；
  分组章过滤后 moduleConfig 呈现真实功能分组（FR_x/子系统）；35 单测全过。
  遗留小项：纯分组章正文（如 XN 性能指标条目）落"默认配置项"，待业务定标是否需要更细归属。
- [x] ~~Word97 .doc 支持~~（曾实测三级转换链通过；**2026-09-28 决策 docx-only，转换链已删除**，前端引导用户转换，9/9 docx 语料回归通过）
- [x] ~~自定义样式标题识别~~（章标题/N级有标题条 已适配）+ 标题尾页码清洗
- [ ] 纯表格驱动文档（无任何标题样式、功能全在表格里）的 S3 兜底——本次 8 篇修复样式后均不再触发，保留 hierarchy_mapping skill 规则 4 作为设计依据
- [ ] Word 自动编号（numbering.xml）回填：8 篇实测文档标题均带显式编号或用 FR_x 命名，未触发；保留防护位
- [ ] regionRate 等默认值已按原型（2.8），若后端将来在请求中传参则请求优先（schema 预留可选参数位）。
- [ ] costDetailList 结构待后端确认修订稿。
- [x] ~~真实 LLM 抽取（S4）实测~~（2026-09-28，MiniMax-M3 @ api.minimaxi.com/anthropic）：
  **skill 体系重构后新基线：8 篇全量 2173 条功能点**（EI 1021/EQ 473/ILF 491/EO 304/EIF 175，明细 `src/evals/baseline/extract_*.json`（评测产物目录，非接口返回） + `scripts/eval_summary.py`），
  旧自由格式基线 547 条仅作历史对照。特征：R10/R11 报价套路生效（操作/数据配比 2.2~2.9，符合"3-5配套"）、
  命名规范与规则溯源（description 含章节+规则号）显著改善；元垃圾（裸 FR_x/"xx逻辑文件"）已加 S5 过滤（7/2173）。
  **关键开放问题——计价口径需业务定标**：接地率检查（实体名在原文的字面命中率）39~68%，
  未接地条目多为套路化衍生（如文档只述"统一认证"，模型按 R10 衍生"新增/修改账号认证信息"）。
  这是基准 skill"报价足量"哲学与 R26"不可凭空编造"的张力：**报价口径（衍生可接受）vs 审计口径（仅字面可追溯）需人工对账定标**。
  其他遗留：① LLM 输出运行间波动（temperature=0.1，评测可置 0）；② 深层级条目（FR_1.1.1.1）与父级语义重复，待定去重口径；
  ③ MiniMax Token Plan 限速——并发≤3 + 20s 退避×3 稳定，生产 RateLimitError 映射为可重试错误。
- [x] 建设方案三工具对比实测（2026-09-28，936页总体方案，各限50段）：
  ① COST_ESTIMATION@功能及性能要求：214 条纯 ILF/EIF（35/15 口径 ✓，费用 553.60 万，公式验算一致）；
  ② NO4_QUOTATION@详细设计：818 条五类，R11 外部导入套路完整呈现（质量最佳）；
  ③ COST_MEASUREMENT@总体设计：262 条，但**架构章节抽取出现元实体噪音**（"功能架构数据/功能架构图数据"——
  架构描述章节不应产功能点，需在 S2 定位规则中排除"架构/组成/拓扑"类章节或 R16/R24 收紧）。
  结论：工具差异化（ILF/EIF vs 五类）由 skill 组合+权重表正确实现；extract_real.py 已支持 --tool。
- [ ] 长轮询 `?wait=30s` 可选项未实现（当前纯短轮询，后端 3s 间隔、15min 放弃）。
- [ ] 二期：通用知识问答 RAG（bge-m3 + sqlite-vec）、审价理由字段、LangGraph 评估。
