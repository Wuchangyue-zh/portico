"""统一错误码。

错误码是能力契约的一部分：Agent 要根据错误码决定"重试 / 换参数 / 停下来问用户"，
所以错误语义必须稳定，不能用 500 糊弄。
"""

from __future__ import annotations


class PorticoError(Exception):
    """所有平台错误的基类。code 会出现在响应体与审计记录中。"""

    code = "INTERNAL"
    http_status = 500

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self, trace_id: str) -> dict:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
                "trace_id": trace_id,
            }
        }


class Unauthorized(PorticoError):
    """身份无效或已过期。调用方应要求重新登录，绝不能猜凭据。"""

    code = "UNAUTHORIZED"
    http_status = 401


class Forbidden(PorticoError):
    """无操作权限或超出数据范围。明确拒绝，绝不重试绕过。"""

    code = "FORBIDDEN"
    http_status = 403


class ValidationError(PorticoError):
    """入参不合法。调用方可根据 schema 修正一次。"""

    code = "VALIDATION_ERROR"
    http_status = 400


class VersionConflict(PorticoError):
    """目标记录已被他人修改。必须重新读取后再提交。"""

    code = "VERSION_CONFLICT"
    http_status = 409


class UpstreamUnavailable(PorticoError):
    """上游暂不可用。可做有界退避重试，并如实告知部分失败。"""

    code = "UPSTREAM_UNAVAILABLE"
    http_status = 503


class RateLimited(PorticoError):
    """超出限流。按 Retry-After 延迟，不要并发轰炸。"""

    code = "RATE_LIMITED"
    http_status = 429


class IdempotencyConflict(PorticoError):
    """同一幂等键对应不同参数。停止调用，需要新的业务指令。"""

    code = "IDEMPOTENCY_CONFLICT"
    http_status = 409


class InvalidContract(PorticoError):
    """能力契约本身不合规。属于发布期错误，不应出现在运行期。"""

    code = "INVALID_CONTRACT"
    http_status = 422
