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


def test_fixed_resolution_is_actionable_only_with_verified_steps() -> None:
    with pytest.raises(ValidationError):
        IssueResolution(
            resolution_found=True,
            disposition_verified=True,
            actionable_fix_available=True,
            resolution_type="fixed",
            root_cause=None,
            resolution_summary="Fixed",
            verified_steps=[],
            evidence_urls=["https://github.com/example/project/pull/1"],
            confidence="high",
        )


def test_issue_141293_disposition_is_verified_but_never_actionable() -> None:
    issue_number = 141293
    resolution = IssueResolution(
        resolution_found=True,
        disposition_verified=True,
        actionable_fix_available=False,
        resolution_type="closed_without_fix",
        root_cause=None,
        resolution_summary="Closed as working as designed",
        verified_steps=[],
        evidence_urls=[
            f"https://github.com/kubernetes/kubernetes/issues/{issue_number}#issuecomment-1"
        ],
        confidence="high",
    )

    assert resolution.disposition_verified is True
    assert resolution.actionable_fix_available is False

    with pytest.raises(ValidationError):
        IssueResolution.model_validate(
            {**resolution.model_dump(), "actionable_fix_available": True}
        )
