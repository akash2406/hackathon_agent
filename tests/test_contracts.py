"""The grounding contract is enforced by construction, not convention."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from crip_backend.contracts import (
    AgentResponse,
    ChatResponse,
    Confidence,
    ConfidenceLevel,
    ResponseStatus,
    Source,
    compose_status,
)

NOW = datetime(2026, 9, 22, tzinfo=UTC)
SOURCE = Source(tool="costpulse_query_costs", api="POST https://management.azure.com/x", invoked_at=NOW, scope="/subscriptions/x")


def grounded(**overrides):
    base = dict(
        agent="costpulse",
        status=ResponseStatus.OK,
        answer="42 USD",
        confidence=Confidence.from_score(0.9),
        query_used="POST ...",
        data_timestamp=NOW,
        sources=[SOURCE],
    )
    base.update(overrides)
    return AgentResponse(**base)


def test_valid_grounded_response():
    assert grounded().grounded


@pytest.mark.parametrize("missing", ["sources", "data_timestamp", "query_used"])
@pytest.mark.parametrize("status", [ResponseStatus.OK, ResponseStatus.PARTIAL])
def test_grounded_status_requires_provenance(status, missing):
    empty = {"sources": [], "data_timestamp": None, "query_used": None}[missing]
    with pytest.raises(ValidationError, match=missing):
        grounded(status=status, **{missing: empty})


def test_no_data_still_requires_the_query_that_found_nothing():
    with pytest.raises(ValidationError):
        grounded(status=ResponseStatus.NO_DATA, sources=[], data_timestamp=None)
    grounded(status=ResponseStatus.NO_DATA, data_timestamp=None)  # sources + query present: fine


def test_error_cannot_carry_numbers_or_confidence():
    with pytest.raises(ValidationError, match="must not carry data"):
        grounded(status=ResponseStatus.ERROR, confidence=Confidence.from_score(0.1), data={"total": 1})
    with pytest.raises(ValidationError, match="low confidence"):
        grounded(status=ResponseStatus.ERROR, confidence=Confidence.from_score(0.9))


def test_confidence_level_and_score_must_agree():
    with pytest.raises(ValidationError):
        Confidence(level=ConfidenceLevel.HIGH, score=0.2)
    assert Confidence.from_score(1.0).level is ConfidenceLevel.HIGH
    assert Confidence.from_score(0.5).level is ConfidenceLevel.MEDIUM
    assert Confidence.from_score(0.0).level is ConfidenceLevel.LOW


def test_sources_can_only_be_user_delegated():
    with pytest.raises(ValidationError):
        Source(tool="t", api="GET x", invoked_at=NOW, scope="/", auth="platform_identity")


def test_data_timestamp_must_be_timezone_aware():
    with pytest.raises(ValidationError):
        grounded(data_timestamp=datetime(2026, 9, 21))


def test_compose_status():
    ok, err = grounded(), grounded(status=ResponseStatus.ERROR, confidence=Confidence.from_score(0.1))
    assert compose_status([]) is ResponseStatus.NO_DATA
    assert compose_status([ok]) is ResponseStatus.OK
    assert compose_status([ok, err]) is ResponseStatus.PARTIAL
    assert compose_status([err]) is ResponseStatus.ERROR


def test_chat_response_cannot_claim_ok_without_grounded_contribution():
    with pytest.raises(ValidationError):
        ChatResponse(
            session_id=uuid4(), message_id=uuid4(), answer="hi", status=ResponseStatus.OK, contributions=[], created_at=NOW
        )
