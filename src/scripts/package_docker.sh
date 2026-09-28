#!/usr/bin/env bash
# 离线交付打包：构建镜像 → docker save → 生产 compose/.env 模板/部署说明/校验和 → 单一 tar.gz
# 用法：bash scripts/package_docker.sh [版本号，默认 0.1.0]
# 产物：dist/pricing-agent-<版本>-<日期>.tar.gz（传输到服务器后解包按 部署说明.md 操作）
set -euo pipefail

cd "$(dirname "$0")/.."
VER="${1:-0.1.0}"
IMAGE="pricing-llm-agent:${VER}"
STAMP="$(date +%Y%m%d)"
DIST="dist/pricing-agent-${VER}-${STAMP}"
IMG_TAR="pricing-llm-agent_${VER}.tar"

echo "== 1/6 检查 Docker 守护进程 =="
docker info >/dev/null 2>&1 || { echo "✗ Docker 未运行，请先启动 Docker Desktop"; exit 1; }
echo "  ✓ Docker 正常"

echo "== 2/6 构建镜像 ${IMAGE} =="
docker build -t "$IMAGE" .

echo "== 3/6 导出镜像（docker save，约需 1-2 分钟）=="
mkdir -p "$DIST"
docker save -o "$DIST/$IMG_TAR" "$IMAGE"

echo "== 4/6 生成生产部署文件 =="
# 生产 compose：剥离开发挂载（DEV-ONLY 标记段）与 build 段，锁定镜像版本
sed -e '/# \[DEV-ONLY BEGIN\]/,/# \[DEV-ONLY END\]/d' \
    -e '/^\s*build: \.\s*$/d' \
    -e "s|image: pricing-llm-agent:.*|image: ${IMAGE}|" \
    docker-compose.yml > "$DIST/docker-compose.yml"

cat > "$DIST/.env.example" <<'EOF'
# ===== 智能体服务 生产配置 =====
# 部署时复制为 .env 并按内网实际填写（.env 不要打包外传）

# 服务间静态鉴权 token（生产必填，与后端约定一致）
AGENT_API_TOKEN=
AGENT_PORT=8600
AGENT_DB_PATH=/app/data/tasks.db
AGENT_WORKERS=4
AGENT_TASK_TTL_HOURS=24

# LLM：内网私有化部署（vLLM/Ollama 的 OpenAI 兼容地址）
LLM_BACKEND=litellm
LLM_MODEL=openai/deepseek-v4-flash
LLM_BASE_URL=http://<内网模型服务地址>/v1
LLM_API_KEY=<内网模型服务密钥，无鉴权可留空>
LLM_TIMEOUT_SECONDS=1200
# 离线环境必开：litellm 直接用内置价格表，跳过对外网 raw.githubusercontent.com 的拉取重试
LITELLM_LOCAL_MODEL_COST_MAP=True

AGENT_MAX_FILE_MB=1024
EOF

cat > "$DIST/部署说明.md" <<'EOF'
# 智能计价·大模型智能体服务 离线部署说明

## 交付物清单

| 文件 | 说明 |
|---|---|
| pricing-llm-agent_<版本>.tar | Docker 镜像（含全部依赖，自包含不依赖镜像仓库） |
| docker-compose.yml | 生产编排（已剥离测试语料挂载，锁镜像版本） |
| .env.example | 配置模板（复制为 .env 后填写；点开头文件在部分工具中默认隐藏） |
| SHA256SUMS.txt | 完整性校验 |

## 部署步骤

```bash
# 0) 前置：服务器已安装 Docker（≥20.10，含 compose 插件）
docker --version && docker compose version

# 1) 解包传输物（若收到的是单一 tar.gz）
tar -xzf pricing-agent-*.tar.gz && cd pricing-agent-*/

# 2) 完整性校验（应无输出差异）
sha256sum -c SHA256SUMS.txt

# 3) 导入镜像
docker load -i pricing-llm-agent_*.tar

# 4) 配置
cp .env.example .env
vi .env      # 必改：AGENT_API_TOKEN、LLM_BASE_URL、LLM_MODEL（内网模型服务）

# 5) 启动（数据存 Docker 命名卷 pricing-agent-task-data：首次挂载自动继承镜像内目录属主，
#    无宿主机权限配置，无需 chown）
docker compose up -d

# 6) 验证
curl http://127.0.0.1:8600/api/v1/llm/health     # 期望 {"status":"ok"}
docker logs -f pricing-llm-agent
```

## 运维要点

- **数据持久化（Docker 命名卷）**：任务库（SQLite，终态结果 TTL 24h 自动过期）存于命名卷
  `pricing-agent-task-data`，卷名跨版本固定——升级到新镜像数据不丢；
  实际路径：`docker volume inspect pricing-agent-task-data --format '{{.Mountpoint}}'`
  （通常为 /var/lib/docker/volumes/pricing-agent-task-data/_data，文件属主 uid 1000）；
  - 备份：`sudo tar czf task-data-backup.tar.gz -C $(docker volume inspect pricing-agent-task-data --format '{{.Mountpoint}}') .`
  - 恢复：解包回上述路径后 `docker compose restart`
  - 彻底清空（慎用）：`docker compose down && docker volume rm pricing-agent-task-data`
- **文件输入**：生产环境 `files.url` 传 MinIO 预签名 URL；本地路径模式仅限联调（需自行挂载目录）；
- **接口契约**：提交 `POST /api/v1/llm/tasks`、查询 `POST /api/v1/llm/tasks/query`，
  鉴权头 `X-Api-Token`（与 .env 中 AGENT_API_TOKEN 一致）；
- **升级**：load 新版本镜像 tar → 修改 compose 的 image 标签 → `docker compose up -d`（任务库兼容）；
- **回滚**：image 标签改回旧版本 → `docker compose up -d`；
- **接口冒烟**（可选，需在部署机有 Python3）：
  `python scripts/api_smoke.py --base http://127.0.0.1:8600 --token <token>`。

## 安全注意

- `.env` 含 token 与内网地址，权限设为 600，禁止随交付物二次外传；
- 服务重启后遗留 PROCESSING 任务自动置 FAILED（errorMsg 注明可重试），由后端重提即可。
EOF

echo "== 5/6 生成校验和与归档 =="
(cd "$DIST" && sha256sum "$IMG_TAR" docker-compose.yml .env.example 部署说明.md > SHA256SUMS.txt)
tar -czf "${DIST}.tar.gz" -C dist "$(basename "$DIST")"

echo "== 6/6 完成 =="
du -sh "$DIST/$IMG_TAR" "${DIST}.tar.gz" | awk '{printf "  %s\t%s\n", $1, $2}'
echo ""
echo "交付物：${DIST}.tar.gz"
echo "服务器端：tar 解包 → sha256sum -c → docker load → cp .env.example .env 并填写 → docker compose up -d"
