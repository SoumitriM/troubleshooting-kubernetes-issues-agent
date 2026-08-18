from typing import Literal, Self
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ResolutionType = Literal[
    "fixed",
    "duplicate",
    "configuration",
    "not_reproducible",
    "closed_without_fix",
    "unknown",
]
Confidence = Literal["low", "medium", "high"]


class IssueResolution(BaseModel):
    """Grounded resolution extracted from collected GitHub evidence."""

    model_config = ConfigDict(extra="forbid")

    resolution_found: bool
    disposition_verified: bool = False
    actionable_fix_available: bool = False
    resolution_type: ResolutionType
    root_cause: str | None = None
    resolution_summary: str | None = None
    verified_steps: list[str] = Field(default_factory=list)
    evidence_urls: list[str] = Field(default_factory=list)
    confidence: Confidence

    @model_validator(mode="before")
    @classmethod
    def derive_legacy_disposition(cls, data: object) -> object:
        if isinstance(data, dict) and "disposition_verified" not in data:
            return {**data, "disposition_verified": bool(data.get("resolution_found"))}
        return data

    @field_validator("evidence_urls")
    @classmethod
    def validate_evidence_urls(cls, urls: list[str]) -> list[str]:
        for url in urls:
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("evidence URLs must be absolute HTTP URLs")
        return list(dict.fromkeys(urls))

    @model_validator(mode="after")
    def validate_resolution_claim(self) -> Self:
        if self.disposition_verified != self.resolution_found:
            raise ValueError("disposition verification must match resolution outcome")
        if self.actionable_fix_available and (
            self.resolution_type not in {"fixed", "configuration"}
            or not self.verified_steps
        ):
            raise ValueError("an actionable fix requires a supported fix and steps")
        if self.resolution_type in {
            "closed_without_fix",
            "duplicate",
            "not_reproducible",
            "unknown",
        } and self.actionable_fix_available:
            raise ValueError("this disposition cannot provide an actionable fix")
        if self.resolution_found:
            if self.resolution_type == "unknown":
                raise ValueError("a found resolution cannot have unknown type")
            if not self.resolution_summary:
                raise ValueError("a found resolution requires a summary")
            if not self.evidence_urls:
                raise ValueError("a found resolution requires evidence")
        else:
            if self.resolution_type != "unknown":
                raise ValueError("an unresolved issue must have unknown type")
            if self.resolution_summary is not None or self.verified_steps:
                raise ValueError("an unresolved issue cannot claim resolution details")
            if self.root_cause is not None and not self.evidence_urls:
                raise ValueError("a root cause requires supporting evidence")
        return self
