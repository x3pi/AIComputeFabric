"""aicf: serverless Python on your own GPU fabric.

    import aicf
    app = aicf.App("demo")

    @app.function(image=aicf.Image.debian_slim().uv_pip_install("numpy"), memory_mb=1024)
    def square(x): return x * x

    print(square.remote(7))          # runs in an isolated sandbox on the fabric
"""
from pathlib import Path
from pkgutil import extend_path

__path__ = extend_path(__path__, __name__)
_gen_aicf = Path(__file__).resolve().parents[3] / "contracts" / "gen" / "python" / "aicf"
if _gen_aicf.is_dir() and str(_gen_aicf) not in __path__:
    __path__.append(str(_gen_aicf))

from aicf.app import (App, ClsHandle, Function, FunctionCall, ModelCache, Sandbox, SandboxInstance, SandboxResult,
                      Secret, Volume, endpoint, enter, exit, function, method, task_queue)
from aicf.client import Client, config_file_path, load_saved_config, save_config
from aicf.errors import (AicfError, AuthError, ConfigError, ConnectionFailed, EnvBuildFailed, FunctionOOM, FunctionTimeout, InvalidRequest,
                         NoCapacity, Preempted, PythonVersionMismatch, QuotaExceeded, RemoteError, ServerError, WorkerLost)
from aicf.image import Image

__version__ = "0.2.0"
__all__ = [
    "App", "ClsHandle", "Function", "FunctionCall", "Image", "Client", "Volume", "ModelCache", "Secret",
    "Sandbox", "SandboxInstance", "SandboxResult",
    "endpoint", "task_queue", "function", "enter", "exit", "method",
    "config_file_path", "load_saved_config", "save_config",
    "AicfError", "AuthError", "ConfigError", "ConnectionFailed", "EnvBuildFailed", "FunctionOOM",
    "FunctionTimeout", "InvalidRequest", "NoCapacity", "Preempted", "PythonVersionMismatch",
    "QuotaExceeded", "RemoteError", "ServerError", "WorkerLost",
]
