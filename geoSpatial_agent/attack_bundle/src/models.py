"""Data models mirroring the paper's notation."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Clue:
    """Typed field-value-confidence triple  q_k = (tau_k, v_k, c_k)."""

    clue_type: str          # tau_k: e.g. "language", "architecture", "vegetation"
    value: str              # v_k
    confidence: float       # c_k in [0, 1]

    def __str__(self) -> str:
        return f"[{self.clue_type}] {self.value} (conf={self.confidence:.2f})"


@dataclass
class EvidencePack:
    """Evidence triple  e = (d, v, w)."""

    description: str = ""             # d: textual location description
    visual_evidence: list[str] = field(default_factory=list)   # v: URLs / paths
    web_evidence: str = ""            # w: POI / map metadata


@dataclass
class Hypothesis:
    """A single beam branch  h = (r, a, E, s)."""

    region: str                                          # r: current region/place
    context: str = ""                                    # a: accumulated textual context
    evidence: list[EvidencePack] = field(default_factory=list)  # E
    confidence: float = 0.0                               # s: confidence / priority
    level: int = 0                                       # current geo-level index
    coordinates: tuple[float, float] | None = None       # resolved lat/lon
    rationale: str = ""

    @property
    def short(self) -> str:
        coord_str = ""
        if self.coordinates:
            coord_str = f" @ ({self.coordinates[0]:.4f}, {self.coordinates[1]:.4f})"
        return f"{self.region}{coord_str} [conf={self.confidence:.2f}]"


@dataclass
class GeoResult:
    """Final output  o = (phi, lambda, y, p, s)."""

    latitude: float
    longitude: float
    description: str
    precision_level: str
    confidence: float
    rationale: str = ""
    hypothesis_chain: list[str] = field(default_factory=list)
    alternatives: list[dict[str, object]] = field(default_factory=list)
    # Optional diagnostic trace populated by v2 pipeline (empty for v1).
    trace: dict = field(default_factory=dict)

    def __str__(self) -> str:
        result = (
            f"Location: {self.description}\n"
            f"Coordinates: ({self.latitude:.6f}, {self.longitude:.6f})\n"
            f"Precision: {self.precision_level}\n"
            f"Confidence: {self.confidence:.2f}\n"
            f"Rationale: {self.rationale}"
        )
        if self.alternatives:
            lines = []
            for i, alt in enumerate(self.alternatives, start=1):
                coords = alt.get("coordinates")
                coord_text = "unknown"
                if isinstance(coords, tuple) and len(coords) == 2:
                    coord_text = f"({coords[0]:.6f}, {coords[1]:.6f})"
                lines.append(
                    f"  {i}. {alt.get('description', 'Unknown')} | "
                    f"conf={float(alt.get('confidence', 0.0)):.2f} | "
                    f"coords={coord_text} | level={alt.get('precision_level', 'unknown')}"
                )
            result += "\nAlternative choices:\n" + "\n".join(lines)
        return result
