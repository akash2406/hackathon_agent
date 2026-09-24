"""Response contracts shared by every tool, agent contribution and API response.

Where this sits in the two flows that matter:

* **A user asking a question** – ``POST /api/chat`` returns a ``ChatResponse``
  whose ``contributions`` are ``AgentResponse`` objects produced by tools; the
  frontend renders their ``query_used`` / ``data_timestamp`` as the citation.
* **A tool call reaching Azure** – every tool handler (e.g. CostPulse's
  ``query_costs``) must return an ``AgentResponse``. That object is what gets
  handed back to the Foundry agent as the tool output, persisted to
  ``agent_invocations`` and shown to the user.

Why grounding is enforced here, at the type level, rather than by convention:
a code-review rule ("remember to attach a source") erodes the moment someone is
in a hurry. A validator cannot be forgotten: it is *impossible* to construct an
``ok``/``partial`` ``AgentResponse`` without sources, a query and a data
timestamp, and impossible to construct an ``error`` response that carries
numbers. Any code path that tries raises ``ValidationError`` before anything
reaches the model, the database or the user. The PostgreSQL schema mirrors the
same rule as a CHECK constraint (defence in depth).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, computed_field, model_validator


class ResponseStatus(StrEnum):
    OK = "ok"  # grounded, complete
    PARTIAL = "partial"  # grounded, with caveats (e.g. truncated paging, mixed currencies)
    NO_DATA = "no_data"  # the query succeeded and found nothing
    ERROR = "error"  # retrieval failed; no numbers may be reported


GROUNDED_STATUSES = frozenset({ResponseStatus.OK, ResponseStatus.PARTIAL})


class ConfidenceLevel(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# Half-open bands [low, high) except HIGH which includes 1.0. Level and score
# must agree so a consumer can trust either field on its own.
_CONFIDENCE_BANDS: dict[ConfidenceLevel, tuple[float, float]] = {
    ConfidenceLevel.HIGH: (0.75, 1.0),
    ConfidenceLevel.MEDIUM: (0.40, 0.75),
    ConfidenceLevel.LOW: (0.0, 0.40),
}


class Confidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    level: ConfidenceLevel
    score: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _level_matches_score(self) -> Confidence:
        low, high = _CONFIDENCE_BANDS[self.level]
        in_band = low <= self.score <= high if self.level is ConfidenceLevel.HIGH else low <= self.score < high
        if not in_band:
            raise ValueError(
                f"confidence level '{self.level}' requires score in [{low}, {high}"
                f"{']' if self.level is ConfidenceLevel.HIGH else ')'}, got {self.score}"
            )
        return self

    @classmethod
    def from_score(cls, score: float) -> Confidence:
        for level, (low, high) in _CONFIDENCE_BANDS.items():
            if low <= score < high or (level is ConfidenceLevel.HIGH and score == high):
                return cls(level=level, score=score)
        raise ValueError(f"score out of range: {score}")


class Source(BaseModel):
    """One concrete Azure API call that backs (or failed to back) an answer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str = Field(min_length=1, description="Backend tool that made the call, e.g. costpulse_query_costs")
    api: str = Field(min_length=1, description="HTTP method + URL of the Azure API called")
    invoked_at: AwareDatetime
    scope: str = Field(min_length=1, description="Azure RBAC scope actually queried")
    # Literal, not str: a Source can only describe a call made with the signed-in
    # user's delegated (OBO) token. A platform-identity call cannot be cited as
    # grounding because it would not reflect what *this user* is allowed to see.
    auth: Literal["user_obo"] = "user_obo"
    request_id: str | None = Field(default=None, description="x-ms-request-id returned by Azure, for support tickets")
    http_status: int | None = None


class AgentResponse(BaseModel):
    """The single contract every agent contribution must conform to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent: str = Field(min_length=1)
    status: ResponseStatus
    answer: str = Field(min_length=1)
    confidence: Confidence
    data: dict[str, Any] | None = None
    query_used: str | None = None
    # When the underlying data was true at the origin (e.g. the latest billing
    # day present in Cost Management), NOT when we called the API.
    data_timestamp: AwareDatetime | None = None
    sources: list[Source] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _enforce_grounding(self) -> AgentResponse:
        if self.status in GROUNDED_STATUSES:
            missing = [
                name
                for name, present in (
                    ("sources", bool(self.sources)),
                    ("data_timestamp", self.data_timestamp is not None),
                    ("query_used", bool(self.query_used)),
                )
                if not present
            ]
            if missing:
                raise ValueError(f"status '{self.status}' is a grounded status and requires: {', '.join(missing)}")
        elif self.status is ResponseStatus.NO_DATA:
            # "no_data" asserts a real query ran and returned nothing, so the
            # query itself must still be citable.
            if not self.sources or not self.query_used:
                raise ValueError("status 'no_data' requires the query_used and sources of the query that returned nothing")
        elif self.status is ResponseStatus.ERROR:
            # A failed retrieval must never carry numbers that look like an answer.
            if self.data is not None:
                raise ValueError("status 'error' must not carry data")
            if self.confidence.level is not ConfidenceLevel.LOW:
                raise ValueError("status 'error' must have low confidence")
        return self

    @property
    def grounded(self) -> bool:
        return self.status in GROUNDED_STATUSES


# --------------------------------------------------------------------------- API layer


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=4000)
    session_id: UUID | None = None


class ChatResponse(BaseModel):
    """Composed answer returned by ``POST /api/chat``.

    ``answer`` is the Orchestrator's composed text. The citation fields are NOT
    taken from that text: ``contributions`` are the exact ``AgentResponse``
    objects returned by the tools, attached by the backend verbatim. The
    Orchestrator model therefore has no opportunity to alter or drop a
    ``query_used`` or ``data_timestamp`` on the way to the user.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: UUID
    message_id: UUID
    answer: str
    status: ResponseStatus
    contributions: list[AgentResponse]
    caveats: list[str] = Field(default_factory=list)
    created_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def grounded(self) -> bool:
        return any(c.grounded for c in self.contributions)

    @model_validator(mode="after")
    def _status_consistent_with_contributions(self) -> ChatResponse:
        if self.status in GROUNDED_STATUSES and not self.grounded:
            raise ValueError("a chat response can only be ok/partial if at least one contribution is grounded")
        return self


def compose_status(contributions: list[AgentResponse]) -> ResponseStatus:
    """Derive the overall status of a composed answer from its contributions.

    No contributions means the Orchestrator answered without retrieving any Azure
    data (e.g. a greeting or a clarifying question). That is reported as
    ``no_data`` together with an explicit "not grounded" caveat, never as ``ok``.
    """
    if not contributions:
        return ResponseStatus.NO_DATA
    statuses = {c.status for c in contributions}
    grounded = statuses & GROUNDED_STATUSES
    if grounded:
        if statuses == {ResponseStatus.OK}:
            return ResponseStatus.OK
        return ResponseStatus.PARTIAL
    if ResponseStatus.ERROR in statuses:
        return ResponseStatus.ERROR
    return ResponseStatus.NO_DATA


class ErrorCode(StrEnum):
    UNAUTHENTICATED = "unauthenticated"
    FORBIDDEN = "forbidden"
    INVALID_REQUEST = "invalid_request"
    SESSION_NOT_FOUND = "session_not_found"
    UNKNOWN_TOOL = "unknown_tool"
    ORCHESTRATOR_UNAVAILABLE = "orchestrator_unavailable"
    UPSTREAM_TIMEOUT = "upstream_timeout"
    PERSISTENCE_UNAVAILABLE = "persistence_unavailable"
    CONFIGURATION_MISSING = "configuration_missing"
    INTERNAL_ERROR = "internal_error"


class ErrorBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str
    correlation_id: str
    retryable: bool = False


class ErrorEnvelope(BaseModel):
    """Every non-2xx response from the API has exactly this shape."""

    model_config = ConfigDict(extra="forbid")

    error: ErrorBody
