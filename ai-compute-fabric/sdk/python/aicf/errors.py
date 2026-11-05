"""Errors raised by the SDK. Every message says what happened AND what to do."""


class AicfError(Exception):
    pass


class ConfigError(AicfError):
    pass


class ConnectionFailed(AicfError):
    pass


class AuthError(AicfError):
    pass


class PythonVersionMismatch(AicfError):
    pass


class QuotaExceeded(AicfError):
    def __init__(self, code: str, message: str, retry_after: int = 0):
        super().__init__(message)
        self.code, self.retry_after = code, retry_after


class NoCapacity(AicfError):
    def __init__(self, message: str, retry_after: int = 0):
        super().__init__(message)
        self.retry_after = retry_after


class InvalidRequest(AicfError):
    pass


class RemoteError(AicfError):
    """The function raised in the sandbox. `.remote_type`, `.remote_message`, `.remote_traceback` carry the original."""

    def __init__(self, remote_type: str, remote_message: str, remote_traceback: str = "", stdout: str = "", stderr: str = ""):
        super().__init__(f"{remote_type}: {remote_message}" + (f"\n--- remote traceback ---\n{remote_traceback}" if remote_traceback else ""))
        self.remote_type, self.remote_message, self.remote_traceback = remote_type, remote_message, remote_traceback
        self.stdout, self.stderr = stdout, stderr


class FunctionTimeout(AicfError):
    pass


class FunctionOOM(AicfError):
    pass


class WorkerLost(AicfError):
    pass


class EnvBuildFailed(AicfError):
    pass


class ServerError(AicfError):
    pass
