# 智能体服务镜像 —— 单进程 uvicorn，内部 asyncio worker 池
# 离线交付：本镜像已包含全部依赖，docker save/load 导入内网即可运行
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    AGENT_DB_PATH=/app/data/tasks.db \
    LITELLM_LOCAL_MODEL_COST_MAP=True

WORKDIR /app

# 依赖层先行（利用构建缓存；pip 走清华镜像，国内网络构建可靠）
# 镜像内安装全部可选依赖（真实管线需要）；开发环境可只装 requirements.txt 核心
# 输入只支持 docx（.doc 由前端引导用户转换），无需 LibreOffice
COPY requirements.txt .
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt \
    && pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple python-docx openpyxl pdfplumber litellm

COPY app ./app

# 非 root 运行（安全评审要求）
RUN useradd -m agent && mkdir -p /app/data && chown -R agent:agent /app
USER agent

EXPOSE 8600

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8600/api/v1/llm/health', timeout=3).status==200 else 1)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8600"]
