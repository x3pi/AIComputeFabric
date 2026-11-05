"""aicf: serverless Python on your own GPU fabric.

    import aicf
    app = aicf.App("demo")

    @app.function(image=aicf.Image.debian_slim().uv_pip_install("numpy"), memory_mb=1024)
    def square(x): return x * x

    print(square.remote(7))          # runs in an isolated sandbox on the fabric
"""
from aicf.app import App, Function
from aicf.client import Client
from aicf.errors import (AicfError, AuthError, ConfigError, ConnectionFailed, EnvBuildFailed, FunctionOOM, FunctionTimeout, InvalidRequest,
                         NoCapacity, PythonVersionMismatch, QuotaExceeded, RemoteError, ServerError, WorkerLost)
from aicf.image import Image

__version__ = "0.1.0"
__all__ = ["App", "Function", "Image", "Client", "AicfError", "AuthError", "ConfigError", "ConnectionFailed", "EnvBuildFailed", "FunctionOOM",
           "FunctionTimeout", "InvalidRequest", "NoCapacity", "PythonVersionMismatch", "QuotaExceeded", "RemoteError", "ServerError", "WorkerLost"]
