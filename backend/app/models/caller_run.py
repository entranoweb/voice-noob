"""Results uploaded from the caller rig (``caller/``), one row per batch and per call.

The caller is a separate command-line package. It places calls into a voice
agent, grades them from the agent's own tool results, and writes a directory of
JSON files. ``synthiq-caller upload`` bundles that directory and posts it here,
which is how a result becomes something a signed-in user can read.

Two rules shape these tables:

* **The raw upload is kept.** The caller's result format is still moving. Every
  column that is not ``raw`` is a reading of it taken at upload time, so a field
  this code did not know about is never lost: it is still in ``raw``.
* **Everything is scoped by workspace.** A run belongs to exactly one workspace,
  and each call repeats the workspace id so a call can be authorised without a
  second query.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class CallerRun(Base, TimestampMixin):
    """One uploaded batch of caller results."""

    __tablename__ = "caller_runs"
    __table_args__ = (
        # The caller chooses the run id, and an upload is idempotent on it within
        # a workspace. Two workspaces may hold the same id without colliding.
        UniqueConstraint("workspace_id", "run_id", name="uq_caller_runs_workspace_run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Workspace that owns this run",
    )
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="User who uploaded the run",
    )

    run_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="Run id chosen by the caller; uploads are idempotent"
    )
    content_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="SHA-256 of the uploaded bundle"
    )
    payload_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, comment="Version of the bundle format as uploaded"
    )

    # What was tested, as the uploader described it.
    rig_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    target_label: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    scenario_set_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Headline counts, worked out from the calls at upload time.
    attempted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    graded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    passed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    not_run: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Calls by outcome as the caller wrote it (passed, failed, void, ungradeable,
    # ungraded, or one this code has not met), plus how many calls used a synthetic
    # voice and how many played a clip whose label no person has confirmed.
    outcome_counts: Mapped[dict[str, int]] = mapped_column(JSON, nullable=False, default=dict)

    # Calls that are not in the pass rate, each with the reason it is not.
    excluded: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    # Chains of calls graded as one unit, as the caller wrote them.
    chains: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    # The bundle as uploaded, without the per-call payloads (those are on the calls).
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict, deferred=True)

    # lazy="raise": these rows can be large, so nothing loads them by accident.
    calls: Mapped[list["CallerCall"]] = relationship(
        "CallerCall",
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
        order_by="CallerCall.position",
    )

    def __repr__(self) -> str:
        return f"<CallerRun {self.id} run_id={self.run_id}>"


class CallerCall(Base, TimestampMixin):
    """One call placed by the caller rig, as graded when it was uploaded."""

    __tablename__ = "caller_calls"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_pk: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("caller_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    position: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Order within the run, from 1"
    )
    file: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    scenario: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    scenario_file: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    mutates: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    repeat: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Kept as the caller wrote it. "passed" and "failed" are the graded outcomes;
    # anything else is a call that proves nothing either way.
    outcome: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    fault_class: Mapped[str | None] = mapped_column(String(60), nullable=True)
    last_step: Mapped[str] = mapped_column(Text, nullable=False, default="")
    conversation_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    ended: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    # A synthetic voice gave at least one answer in this call.
    synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # At least one clip played had a label no person has confirmed.
    labels_unverified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Said, heard and stored for each field, with where "stored" came from.
    fields: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    timeline: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list, deferred=True
    )
    tool_calls: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list, deferred=True
    )
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict, deferred=True)

    run: Mapped["CallerRun"] = relationship("CallerRun", back_populates="calls", lazy="raise")

    def __repr__(self) -> str:
        return f"<CallerCall {self.id} {self.scenario!r} {self.outcome}>"
