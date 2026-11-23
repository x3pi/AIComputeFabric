"""CAD backend interface. The agent, the verifier and the benchmark only talk to this; a backend turns code into a solid and measures it.
Backends: cq_backend.CadQueryBackend (headless, Linux/Windows/Mac). A SolidWorks backend can implement the same methods later."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict


@dataclass
class Measure:
    volume: float                 # mm3
    bbox_size: list               # [dx, dy, dz] mm
    bbox_min: list
    bbox_max: list
    com: list                     # centre of mass, mm
    faces: int
    edges: int
    vertices: int
    solids: int
    valid: bool
    links: str = ""               # how the solids connect: "" for one solid, else groups and FLOATING parts with their gap in mm

    def as_dict(self):
        return asdict(self)

    def text(self):
        r = lambda v: [round(x, 3) for x in v]
        return ("volume=%.4f mm3 | bbox size=%s min=%s max=%s | centre of mass=%s | solids=%d faces=%d edges=%d vertices=%d | valid=%s"
                % (self.volume, r(self.bbox_size), r(self.bbox_min), r(self.bbox_max), r(self.com), self.solids, self.faces, self.edges, self.vertices, self.valid)) + ((" | connectivity: " + self.links) if self.links else "")


@dataclass
class ExecResult:
    ok: bool
    output: str = ""              # what the code printed
    error: str = ""               # short error text (exception type + message + the offending line)
    measure: Measure = None       # of the part after this cell (None if there is no part yet)
    delta: dict = None            # what the cell removed/added, as kernel-measured regions {removed:{volume,min,max}|None, added:...}
    changed: bool = False         # the part differs from before this cell (new/modified shape)


class Backend(ABC):
    name = "abstract"

    @abstractmethod
    def execute(self, code: str) -> ExecResult: ...
    @abstractmethod
    def measure(self) -> Measure: ...
    @abstractmethod
    def undo(self) -> bool:
        """Drop the last successful cell. -> False when there is nothing to undo."""
    @abstractmethod
    def reset(self): ...
    @abstractmethod
    def export_step(self, path: str) -> str: ...
    def preview_svg(self) -> str:
        raise NotImplementedError("this backend has no preview")

    @abstractmethod
    def close(self): ...
