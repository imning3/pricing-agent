"""统一错误类型与 errorMsg 约定。

对外只暴露 errorMsg 文本（契约无错误码字段）；code 仅用于内部日志与测试断言。
"""
from __future__ import annotations


class AgentError(Exception):
    """管线可预期失败，转 FAILED。"""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class FileDownloadError(AgentError):
    def __init__(self, message: str):
        super().__init__("E_FILE_DOWNLOAD", message, retryable=False)


class FileFormatError(AgentError):
    def __init__(self, message: str):
        super().__init__("E_FILE_FORMAT", message, retryable=False)


class LLMError(AgentError):
    def __init__(self, message: str, retryable: bool = True):
        super().__init__("E_LLM_ERROR", message, retryable=retryable)


class ValidationError(AgentError):
    def __init__(self, message: str):
        super().__init__("E_SCHEMA_VALIDATION", message, retryable=False)
