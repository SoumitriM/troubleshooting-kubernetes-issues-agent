import pytest
from pydantic import ValidationError

from app.enrichment.resolution_models import IssueResolution


def test_positive_resolution_requires_summary_and_evidence_url() -> None:
    with pytest.raises(ValidationError):
        IssueResolution(resolution_found=True, resolution_type="fixed", root_cause=None,
            resolution_summary="Fixed by a commit", verified_steps=[],
            evidence_urls=[], confidence="high")


def test_unresolved_issue_cannot_claim_resolution_steps() -> None:
    with pytest.raises(ValidationError):
        IssueResolution(resolution_found=False, resolution_type="unknown",
            root_cause=None, resolution_summary=None,
            verified_steps=["Upgrade kubelet"], evidence_urls=[], confidence="low")


def test_supported_root_cause_can_exist_without_resolution() -> None:
    resolution = IssueResolution(resolution_found=False, resolution_type="unknown",
        root_cause="Configuration mismatch", resolution_summary=None,
        verified_steps=[],
        evidence_urls=["https://github.com/example/project/issues/1#issuecomment-1"],
        confidence="medium")
    assert resolution.root_cause == "Configuration mismatch"
