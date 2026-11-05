"""Image = the Python environment a function runs in. Wheels only; hashes are pinned by the builder, never by the client."""
from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass

from aicf.errors import PythonVersionMismatch

SUPPORTED_PYTHON = "3.12"


@dataclass(frozen=True)
class Image:
    python_version: str = SUPPORTED_PYTHON
    packages: tuple = ()
    cuda_tag: str = ""
    env: tuple = ()                  # (("KEY", "value"), ...)
    local_modules: tuple = ()        # names of local modules shipped BY VALUE with the function

    @classmethod
    def debian_slim(cls, python_version: str = SUPPORTED_PYTHON) -> "Image":
        return cls(python_version=python_version)

    def uv_pip_install(self, *packages: str) -> "Image":
        return Image(self.python_version, tuple(self.packages) + packages, self.cuda_tag, self.env, self.local_modules)

    pip_install = uv_pip_install

    def with_cuda(self, tag: str) -> "Image":
        return Image(self.python_version, self.packages, tag, self.env, self.local_modules)

    def env_vars(self, mapping: dict) -> "Image":
        return Image(self.python_version, self.packages, self.cuda_tag, tuple(self.env) + tuple(sorted(mapping.items())), self.local_modules)

    def add_local_python_source(self, *modules: str) -> "Image":
        """Ship these local modules by value (cloudpickle), so the sandbox needs no access to your filesystem."""
        return Image(self.python_version, self.packages, self.cuda_tag, self.env, tuple(self.local_modules) + modules)

    def spec(self) -> dict:
        h = hashlib.sha256("\n".join(self.local_modules).encode()).hexdigest() if self.local_modules else ""
        return {"python_version": self.python_version, "packages": list(self.packages), "cuda_tag": self.cuda_tag, "local_source_hash": h}

    def check_local_python(self) -> None:
        local = f"{sys.version_info.major}.{sys.version_info.minor}"
        if local != self.python_version:
            raise PythonVersionMismatch(
                f"your local Python is {local} but the image uses {self.python_version}. Functions are shipped as pickles and need the same minor "
                f"version: run with Python {self.python_version} (e.g. `uv run --python {self.python_version} ...`).")
