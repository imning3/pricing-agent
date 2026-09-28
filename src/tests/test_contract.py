# -*- coding: utf-8 -*-
"""契约层：字段校验、枚举拒绝、序列化形态。"""
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas.contract import LlmRequest, QueryResponse, TaskStatus


def _body() -> dict:
    return {
        "calculationId": "abc",
        "type": "analysis",
        "projectName": "P",
        "toolCode": "NO4_QUOTATION",
        "files": [{"fileName": "a.docx", "fileType": "docx",
                   "category": "TECH_REQ", "url": "http://x/a.docx"}],
    }


def test_request_ok():
    req = LlmRequest.model_validate(_body())
    assert req.toolCode.value == "NO4_QUOTATION"
    assert req.originalFpList == []


def test_category_enum_strict():
    """定义优先：文档示例里的 TECH_REQUIREMENT 是笔误，必须拒绝。"""
    body = _body()
    body["files"][0]["category"] = "TECH_REQUIREMENT"
    with pytest.raises(ValidationError):
        LlmRequest.model_validate(body)


def test_unknown_toolcode_rejected():
    body = _body()
    body["toolCode"] = "REAL_COST"  # 旧枚举已废弃
    with pytest.raises(ValidationError):
        LlmRequest.model_validate(body)


def test_extra_fields_rejected():
    body = _body()
    body["session"] = "x"
    with pytest.raises(ValidationError):
        LlmRequest.model_validate(body)


def test_query_response_expired_shape():
    """任务不存在：status=None + errorMsg，HTTP 仍 200 的契约形态。"""
    resp = QueryResponse(calculationId="abc", status=None, result=None, errorMsg="任务不存在或已过期")
    d = resp.model_dump(mode="json")
    assert d["status"] is None and d["result"] is None
    assert d["errorMsg"] == "任务不存在或已过期"
