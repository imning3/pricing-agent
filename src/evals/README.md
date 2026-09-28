# evals/ —— 评测产物（不是接口返回数据）

本目录存放**评测基线产物**，与接口契约（`app/schemas/contract.py`、《智能计价-大模型任务接口文档0921.docx》）无关：

- `baseline/extract_<文档>_<工具>.json`：`scripts/extract_real.py` 对真实测试文档跑 S4 抽取的
  基线快照。外层字段（file/model/tool/segments/summary）是评测元数据；其中 `fpList` 数组
  的元素字段与契约 originalFpList 元素一致，但**整体不是 LlmResponse 结构**。
- 接口的真实返回形态以 `schemas/contract.py` 为准，冒烟验证用 `scripts/api_smoke.py`。

重新生成基线：`python scripts/extract_real.py <关键词> --tool TOOLCODE --filter <章节正则>`
汇总对照表：`python scripts/eval_summary.py`
