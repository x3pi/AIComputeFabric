"""One exception type for every deliberate HTTP error, so handlers never build responses by hand."""
from __future__ import annotations


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def bad(message: str, code: str = "bad_request") -> ApiError:
    return ApiError(400, code, message)


def forbidden(message: str) -> ApiError:
    return ApiError(403, "forbidden", message)


def not_found(what: str) -> ApiError:
    return ApiError(404, "not_found", f"{what} not found")


def conflict(message: str, code: str = "conflict") -> ApiError:
    return ApiError(409, code, message)


def too_large(message: str) -> ApiError:
    return ApiError(413, "too_large", message)


def quota(message: str) -> ApiError:
    return ApiError(429, "quota_exceeded", message)


def unavailable(message: str) -> ApiError:
    return ApiError(503, "unavailable", message)
