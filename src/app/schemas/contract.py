"""接口契约模型 —— 单一事实来源。

依据《智能计价-大模型任务接口文档0921.docx》+ 双方决议：
- 定义与示例冲突时以定义为准；
- 金额单位全部为元（内部万元计算，序列化前 ×10000，见 engine/formulas.py）；
- auditPriceList.moduleConfig 必须与 originalFpList 一致。
"""
from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------- 枚举 ----------

class ToolCode(str, Enum):
    COST_ESTIMATION = "COST_ESTIMATION"                # 经费估算工具（竞标决策）
    NO4_QUOTATION = "NO4_QUOTATION"                    # 四号文报价工具（投标）
    COST_MEASUREMENT = "COST_MEASUREMENT"              # 真实成本测算工具（全要素策划）
    NO4_AUDIT = "NO4_AUDIT"                            # 四号文审价工具（外协谈价）
    NO4_QUOTATION_REVIEW = "NO4_QUOTATION_REVIEW"      # 四号文报价工具（需求评审/可信四）


class TaskType(str, Enum):
    ANALYSIS = "analysis"
    CONVERSATION = "conversation"


class TaskStatus(str, Enum):
    PROCESSING = "PROCESSING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class FileCategory(str, Enum):
    TECH_REQ = "TECH_REQ"                    # 技术需求
    TECH_PROPOSAL = "TECH_PROPOSAL"          # 技术方案
    DETAILED_TECH_REQ = "DETAILED_TECH_REQ"  # 细化后技术要求
    OVERALL_TECH_PLAN = "OVERALL_TECH_PLAN"  # 总体技术方案
    OUTSOURCE_TASK = "OUTSOURCE_TASK"        # 外协任务书
    OUTSOURCE_QUOTE = "OUTSOURCE_QUOTE"      # 外协单位报价文件
    REQ_SPEC = "REQ_SPEC"                    # 需求规格说明


class FpType(str, Enum):
    ILF = "ILF"
    EIF = "EIF"
    EI = "EI"
    EO = "EO"
    EQ = "EQ"


class ChatRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"


# ---------- 请求 ----------

class LlmFileInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fileName: str
    fileType: str = Field(description="文件类型（扩展名）")
    category: FileCategory
    url: str = Field(description="文件预签名 URL")


class LlmChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: ChatRole
    content: str


class LlmRequest(BaseModel):
    """接口一请求体。历史上下文五类列表首轮为空、对话时不为空。"""

    model_config = ConfigDict(extra="forbid")

    calculationId: str = Field(min_length=1, max_length=128)
    type: TaskType
    projectName: str
    projectDescription: Optional[str] = None
    toolCode: ToolCode
    targetMaxPrice: Optional[Decimal] = None
    files: List[LlmFileInfo] = Field(default_factory=list)
    chatHistory: List[LlmChatMessage] = Field(default_factory=list)
    # 历史业务上下文（对话时后端全量传回）
    originalFpList: List["OriginalFp"] = Field(default_factory=list)
    fpScaleList: List["FpScale"] = Field(default_factory=list)
    costItemList: List["CostItem"] = Field(default_factory=list)
    costDetailList: List["CostDetail"] = Field(default_factory=list)
    auditPriceList: List["AuditPrice"] = Field(default_factory=list)


# ---------- result 元素 ----------

class OriginalFp(BaseModel):
    model_config = ConfigDict(extra="forbid")

    moduleSystem: str = Field(description="子系统")
    moduleConfig: str = Field(description="软件（配置项）")
    softwareObject: str = Field(description="功能块（软件估算对象）")
    requirementName: str = Field(description="功能需求")
    fpType: FpType
    fpCount: Decimal = Field(description="功能点个数（权重折算在公式引擎）")
    description: Optional[str] = None
    auditResult: Optional[int] = Field(default=None, description="1-合理 0-不合理，仅审价工具")


class FpScale(BaseModel):
    model_config = ConfigDict(extra="forbid")

    moduleSystem: str
    moduleConfig: str
    softwareObject: str
    originalFp: Decimal
    reuseFactor: Decimal
    f1Criticality: Decimal
    f2Distributed: Decimal
    f3Performance: Decimal
    f4Resource: Decimal
    f5Complex: Decimal
    f6Reusability: Decimal
    f7MultiEnv: Decimal
    adjustFactor: Decimal
    adjustedFp: Decimal


class CostItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    moduleConfig: str = Field(description="计价对象=软件（配置项）")
    fpScale: Decimal = Field(description="软件功能规模（该配置项调整后功能点合计）")
    workload: Decimal = Field(description="工作量（人月）")
    regionRate: Decimal = Field(description="地区费率（万元/人月，按原型默认 2.8）")
    allocationFee: Decimal = Field(description="分摊国拨事业费")
    calculatedAmount: Decimal = Field(description="测算金额（元）")
    talentSalary: Decimal = Field(description="高端人才薪资及劳务费（元）")
    subtotal: Decimal = Field(description="小计（元）")


class AuditPrice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    moduleConfig: str = Field(description="须与 originalFpList 中 moduleConfig 一致")
    fpScaleQuotation: Decimal
    outsourcePrice: Decimal = Field(description="外协报价（元）")
    fpScaleAudit: Decimal
    auditPrice: Decimal = Field(description="审价结果（元）")


class CostDetail(BaseModel):
    """文档 3.4.6 原定义自相矛盾（文档自注"此节有问题"），本结构为已提交后端的修订稿，待确认。"""

    model_config = ConfigDict(extra="forbid")

    moduleSystem: str
    moduleConfig: str
    softwareObject: str
    requirementName: str
    cost: Decimal = Field(description="成本（元）")
    outsourceUnitCost: Optional[Decimal] = Field(default=None, description="外协单位成本（元），null=自研")
    outsourceUnit: Optional[str] = Field(default=None, description="外协单位，null=自研")


# ---------- 响应 ----------

class SubmitResponse(BaseModel):
    calculationId: str
    status: TaskStatus = TaskStatus.PROCESSING


class LlmResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: Optional[str] = None
    originalFpList: List[OriginalFp] = Field(default_factory=list)
    fpScaleList: List[FpScale] = Field(default_factory=list)
    costItemList: List[CostItem] = Field(default_factory=list)
    auditPriceList: Optional[List[AuditPrice]] = None   # 仅 NO4_AUDIT
    costDetailList: Optional[List[CostDetail]] = None   # 仅 COST_MEASUREMENT


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    calculationId: str = Field(min_length=1, max_length=128)


class QueryResponse(BaseModel):
    """任务不存在/已过期时 status=None 且 errorMsg='任务不存在或已过期'（HTTP 仍 200）。"""

    calculationId: str
    status: Optional[TaskStatus] = None
    result: Optional[LlmResponse] = None
    errorMsg: Optional[str] = None


LlmRequest.model_rebuild()
