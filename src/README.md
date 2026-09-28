# 智能计价 · 大模型智能体服务

接口契约见《智能计价-大模型任务接口文档0921.docx》；项目知识基线见仓库根 `AGENTS.md`（改契约/公式/架构前必读）。

## 快速开始

### 方式一：Docker（推荐，交付形态）

```bash
cd src
cp .env.example .env          # 按需修改，默认 mock 模式无需任何 LLM 配置
docker compose up -d          # 构建并启动，端口 8600
```

内网离线交付（目标环境无外网/无镜像仓库）：

```bash
# 打包（产物 dist/pricing-agent-<版本>-<日期>.tar.gz，含镜像+生产compose+.env模板+部署说明+校验和）
bash scripts/package_docker.sh [版本号，默认 0.1.0]

# 服务器端：解包 → 校验 → 导入 → 配置 → 启动（详见包内 部署说明.md）
tar -xzf pricing-agent-*.tar.gz && cd pricing-agent-*/
sha256sum -c SHA256SUMS.txt
docker load -i pricing-llm-agent_*.tar
cp .env.example .env && vi .env      # 必改 AGENT_API_TOKEN / LLM_BASE_URL / LLM_MODEL
docker compose up -d
```

### 方式二：本机直跑（开发调试）

```bash
conda activate pricing-agent      # 专用环境（Python 3.12，依赖已装齐）
cd src
python -m pytest tests/ -q        # 回归
python -m app.main                # 或 uvicorn app.main:app --port 8600
```

## 冒烟（mock 模式）

```bash
curl -X POST http://127.0.0.1:8600/api/v1/llm/tasks -H "Content-Type: application/json" -d @- <<'EOF'
{
  "calculationId": "smoke-001",
  "type": "analysis",
  "projectName": "某指挥信息系统",
  "toolCode": "NO4_QUOTATION",
  "files": [{"fileName": "技术要求.docx", "fileType": "docx", "category": "TECH_REQ", "url": "http://example.invalid/a.docx"}]
}
EOF

curl -X POST http://127.0.0.1:8600/api/v1/llm/tasks/query -H "Content-Type: application/json" \
  -d '{"calculationId": "smoke-001"}'
```

## 接口说明

依据《智能计价-大模型任务接口文档0921.docx》+ 双方决议实现（详见仓库根 AGENTS.md §2）。

### 端点

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/v1/llm/tasks` | POST | 提交任务（analysis 触发测算 / conversation 对话），立即返回 `PROCESSING` |
| `/api/v1/llm/tasks/query` | POST | 查询任务，轮询到终态（建议 3s 间隔、15min 放弃） |
| `/api/v1/llm/health` | GET | 健康检查 |

鉴权：请求头 `X-Api-Token`（env `AGENT_API_TOKEN`；为空不校验，仅限开发）。参数校验失败返回 HTTP 400 + `{"errorMsg": ...}`。

### 1. 提交任务 `POST /api/v1/llm/tasks`

```json
{
  "calculationId": "9f2c1b74e5a84d3c8b1f0a6d2e7c4b91",
  "type": "analysis",
  "projectName": "二期05数据模拟软件",
  "projectDescription": "可选",
  "toolCode": "NO4_QUOTATION_REVIEW",
  "targetMaxPrice": null,
  "files": [
    {"fileName": "二期05.docx", "fileType": "docx", "category": "REQ_SPEC",
     "url": "https://minio.../presigned-url（联调可用本地路径，如 /data/testfiles/xx.docx）"}
  ],
  "chatHistory": [],
  "originalFpList": [], "fpScaleList": [], "costItemList": [],
  "costDetailList": [], "auditPriceList": []
}
```

- `calculationId`：调用方生成，任务唯一标识；**重复提交幂等**（回显当前状态，不重复执行）。
- `type`：`analysis`（触发测算）| `conversation`（对话，需带 `chatHistory` 与五类历史列表）。
- `toolCode`：`COST_ESTIMATION`（经费估算/竞标决策）/ `NO4_QUOTATION`（四号文报价/投标）/
  `COST_MEASUREMENT`（真实成本测算/全要素策划）/ `NO4_AUDIT`（四号文审价/外协谈价）/
  `NO4_QUOTATION_REVIEW`（四号文报价/需求评审）。`targetMaxPrice` 仅 NO4_QUOTATION 使用。
- `files[].category`：`TECH_REQ / TECH_PROPOSAL / DETAILED_TECH_REQ / OVERALL_TECH_PLAN / OUTSOURCE_TASK / OUTSOURCE_QUOTE / REQ_SPEC`。
- **文档类输入只支持 docx**（.doc 等请前端引导用户转换；审价报价文件支持 xlsx）。

响应（恒 200）：

```json
{"calculationId": "9f2c...", "status": "PROCESSING"}
```

### 2. 查询任务 `POST /api/v1/llm/tasks/query`

```json
{"calculationId": "9f2c..."}
```

响应（终态保留 24h，过期或不存在时 `status=null`）：

```json
{
  "calculationId": "9f2c...",
  "status": "SUCCESS",
  "errorMsg": null,
  "result": {
    "content": "已完成识别与测算：125 条功能需求、6 个配置项、综合费用合计 80.08 万元。",
    "originalFpList": [
      {"moduleSystem": "二期05数据模拟软件", "moduleConfig": "登录功能（FR_1）",
       "softwareObject": "登录功能（FR_1）", "requirementName": "用户登录信息",
       "fpType": "ILF", "fpCount": 7,
       "description": "依据FR_1登录功能章节；R1/R3", "auditResult": null}
    ],
    "fpScaleList": [{"moduleSystem": "...", "moduleConfig": "...", "softwareObject": "...",
      "originalFp": 157.00, "reuseFactor": 1.00, "f1Criticality": 0, "f2Distributed": 0,
      "f3Performance": 0, "f4Resource": 0, "f5Complex": 0, "f6Reusability": 0, "f7MultiEnv": 0,
      "adjustFactor": 1.00, "adjustedFp": 157.00}],
    "costItemList": [{"moduleConfig": "...", "fpScale": 157.00, "workload": 7.55,
      "regionRate": 2.8, "allocationFee": 0, "calculatedAmount": 211390.91,
      "talentSalary": 0, "subtotal": 211390.91}],
    "auditPriceList": null,
    "costDetailList": null
  }
}
```

- `status`：`PROCESSING` / `SUCCESS` / `FAILED`（失败时 `errorMsg` 给原因，如文件不存在/格式不符/模型超时）。
- `fpCount` 口径：**原始功能点数 = 标准权重 × 个数**（四号文系 ILF 7/EIF 5/EI 4/EO 5/EQ 4，
  经费估算 ILF 35/EIF 15）；估算对象原始功能点 = Σ fpCount 简单求和。
- **金额单位全部为元**；`fpScaleList` 按 softwareObject 粒度、`costItemList` 按 moduleConfig 粒度。
- `auditPriceList` 仅 NO4_AUDIT 返回（`auditResult` 逐条 1 合理/0 不合理）；
  `costDetailList` 仅 COST_MEASUREMENT 返回。
- `conversation` 修改功能点后：**全量返回**重算的 fpScaleList/costItemList（后端全量替换），
  回复含【已应用】/【未应用】编辑摘要。

## 测试

```bash
python -m pytest tests/ -q
```

### 眞实文档管线探针

对真实需规文档实弹跑 S0→S3 + mock 端到端（不联网、不调 LLM）：

```bash
python scripts/probe_docs.py "../测试文件"            # 全部文件统计报告
python scripts/probe_docs.py "../测试文件" --tree 01  # 打印指定文件的章节树
```

Word97 `.doc` 转换：容器内自带 LibreOffice；本机开发用 Word COM 兜底（或设 `SOFFICE_PATH`）。

## 模式说明

- `LLM_BACKEND=mock`：不联网、不调模型，用确定性样例数据走完整管线（联调契约/演示用）。
- `LLM_BACKEND=litellm`：真实管线（下载解析文档 + LLM 抽取），需安装可选依赖 litellm/markitdown/openpyxl 等。
