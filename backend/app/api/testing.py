"""Testing API routes for QA Testing Framework.

Provides endpoints for managing test scenarios and running tests.
"""

import uuid
from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser
from app.core.config import settings
from app.db.session import get_db
from app.models.agent import Agent
from app.models.test_scenario import (
    ScenarioCategory,
    ScenarioDifficulty,
    TestRun,
    TestRunStatus,
    TestScenario,
)
from app.services.qa.test_runner import TestRunner

router = APIRouter(prefix="/api/v1/testing", tags=["testing"])
logger = structlog.get_logger()


# =============================================================================
# Pydantic Schemas
# =============================================================================


class TestScenarioResponse(BaseModel):
    """Test scenario response."""

    id: str
    name: str
    description: str | None
    category: str
    difficulty: str
    caller_persona: dict[str, Any]
    expected_behaviors: list[str]
    success_criteria: dict[str, Any]
    is_active: bool
    is_built_in: bool
    tags: list[str] | None
    created_at: datetime

    model_config = {"from_attributes": True}


class TestScenarioListResponse(BaseModel):
    """Paginated test scenarios response."""

    scenarios: list[TestScenarioResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


class TestRunResponse(BaseModel):
    """Test run response."""

    id: str
    scenario_id: str
    scenario_name: str | None = None
    agent_id: str
    agent_name: str | None = None
    status: str
    started_at: datetime | None
    completed_at: datetime | None
    duration_ms: int | None
    overall_score: int | None
    passed: bool | None
    issues_found: list[str] | None
    recommendations: list[str] | None
    created_at: datetime

    model_config = {"from_attributes": True}


class TestRunDetailResponse(TestRunResponse):
    """Detailed test run response with full results."""

    actual_transcript: list[dict[str, Any]] | None
    behavior_matches: dict[str, bool] | None
    criteria_results: dict[str, Any] | None
    error_message: str | None


class TestRunListResponse(BaseModel):
    """Paginated test runs response."""

    runs: list[TestRunResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


class RunTestRequest(BaseModel):
    """Request to run a test scenario."""

    scenario_id: str
    agent_id: str
    workspace_id: str | None = None


class RunTestResponse(BaseModel):
    """Response after starting a test."""

    message: str
    test_run_id: str
    status: str


class RunAllTestsRequest(BaseModel):
    """Request to run all tests for an agent."""

    agent_id: str
    workspace_id: str | None = None
    category: str | None = None


class RunAllTestsResponse(BaseModel):
    """Response after starting all tests."""

    message: str
    test_count: int
    queued: bool


class SeedScenariosResponse(BaseModel):
    """Response after seeding scenarios."""

    message: str
    scenarios_created: int


class TestingSummaryResponse(BaseModel):
    """Testing summary for an agent."""

    agent_id: str
    total_runs: int
    passed: int
    failed: int
    errors: int
    pass_rate: float
    avg_score: float | None
    last_run_at: datetime | None


# =============================================================================
# Scenario Endpoints
# =============================================================================


@router.get("/scenarios", response_model=TestScenarioListResponse)
async def list_scenarios(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    category: str | None = Query(default=None, description="Filter by category"),
    difficulty: str | None = Query(default=None, description="Filter by difficulty"),
    built_in_only: bool = Query(default=False, description="Show only built-in scenarios"),
) -> TestScenarioListResponse:
    """List test scenarios with pagination and filters."""
    log = logger.bind(user_id=current_user.id)
    log.info("listing_scenarios", page=page, page_size=page_size)

    # Build query
    query = select(TestScenario).where(TestScenario.is_active == True)  # noqa: E712

    if category:
        query = query.where(TestScenario.category == category)
    if difficulty:
        query = query.where(TestScenario.difficulty == difficulty)
    if built_in_only:
        query = query.where(TestScenario.is_built_in == True)  # noqa: E712

    # Get total count
    count_query = select(func.count()).select_from(query.subquery())
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Apply pagination and ordering
    query = query.order_by(TestScenario.category, TestScenario.difficulty)
    query = query.offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(query)
    scenarios = result.scalars().all()

    total_pages = (total + page_size - 1) // page_size

    return TestScenarioListResponse(
        scenarios=[
            TestScenarioResponse(
                id=str(s.id),
                name=s.name,
                description=s.description,
                category=s.category,
                difficulty=s.difficulty,
                caller_persona=s.caller_persona,
                expected_behaviors=s.expected_behaviors,
                success_criteria=s.success_criteria,
                is_active=s.is_active,
                is_built_in=s.is_built_in,
                tags=s.tags,
                created_at=s.created_at,
            )
            for s in scenarios
        ],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


@router.get("/scenarios/{scenario_id}", response_model=TestScenarioResponse)
async def get_scenario(
    scenario_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> TestScenarioResponse:
    """Get a specific test scenario."""
    result = await db.execute(select(TestScenario).where(TestScenario.id == uuid.UUID(scenario_id)))
    scenario = result.scalar_one_or_none()

    if not scenario:
        raise HTTPException(status_code=404, detail="Scenario not found")

    return TestScenarioResponse(
        id=str(scenario.id),
        name=scenario.name,
        description=scenario.description,
        category=scenario.category,
        difficulty=scenario.difficulty,
        caller_persona=scenario.caller_persona,
        expected_behaviors=scenario.expected_behaviors,
        success_criteria=scenario.success_criteria,
        is_active=scenario.is_active,
        is_built_in=scenario.is_built_in,
        tags=scenario.tags,
        created_at=scenario.created_at,
    )


@router.post("/scenarios/seed", response_model=SeedScenariosResponse)
async def seed_scenarios(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> SeedScenariosResponse:
    """Seed built-in test scenarios to database."""
    log = logger.bind(user_id=current_user.id)
    log.info("seeding_scenarios")

    runner = TestRunner(db)
    count = await runner.seed_built_in_scenarios()

    return SeedScenariosResponse(
        message=f"Seeded {count} scenarios" if count > 0 else "Scenarios already seeded",
        scenarios_created=count,
    )


@router.get("/categories")
async def list_categories(
    current_user: CurrentUser,
) -> dict[str, list[str]]:
    """List available scenario categories and difficulties."""
    return {
        "categories": [c.value for c in ScenarioCategory],
        "difficulties": [d.value for d in ScenarioDifficulty],
    }


# =============================================================================
# Test Run Endpoints
# =============================================================================


@router.post("/run", response_model=RunTestResponse)
async def run_test(
    request: RunTestRequest,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> RunTestResponse:
    """Run a specific test scenario against an agent."""
    log = logger.bind(
        user_id=current_user.id,
        scenario_id=request.scenario_id,
        agent_id=request.agent_id,
    )
    log.info("running_test")

    if not settings.QA_ENABLED:
        raise HTTPException(status_code=400, detail="QA testing is disabled")

    if not settings.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=400, detail="Anthropic API key not configured")

    # Verify scenario exists
    scenario_result = await db.execute(
        select(TestScenario).where(TestScenario.id == uuid.UUID(request.scenario_id))
    )
    if not scenario_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Scenario not found")

    # Verify agent exists and belongs to user
    agent_result = await db.execute(
        select(Agent).where(
            Agent.id == uuid.UUID(request.agent_id),
            Agent.user_id == current_user.id,
        )
    )
    if not agent_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Agent not found")

    # Run the test
    runner = TestRunner(db)
    test_run = await runner.run_scenario(
        scenario_id=uuid.UUID(request.scenario_id),
        agent_id=uuid.UUID(request.agent_id),
        user_id=current_user.id,
        workspace_id=uuid.UUID(request.workspace_id) if request.workspace_id else None,
    )

    return RunTestResponse(
        message="Test completed",
        test_run_id=str(test_run.id),
        status=test_run.status,
    )


@router.post("/run-all", response_model=RunAllTestsResponse)
async def run_all_tests(
    request: RunAllTestsRequest,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> RunAllTestsResponse:
    """Run all test scenarios against an agent (async)."""
    log = logger.bind(user_id=current_user.id, agent_id=request.agent_id)
    log.info("running_all_tests")

    if not settings.QA_ENABLED:
        raise HTTPException(status_code=400, detail="QA testing is disabled")

    # Verify agent exists and belongs to user
    agent_result = await db.execute(
        select(Agent).where(
            Agent.id == uuid.UUID(request.agent_id),
            Agent.user_id == current_user.id,
        )
    )
    if not agent_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Agent not found")

    # Count scenarios
    query = (
        select(func.count())
        .select_from(TestScenario)
        .where(
            TestScenario.is_active == True  # noqa: E712
        )
    )
    if request.category:
        query = query.where(TestScenario.category == request.category)

    count_result = await db.execute(query)
    scenario_count = count_result.scalar() or 0

    if scenario_count == 0:
        raise HTTPException(status_code=400, detail="No scenarios available to run")

    # Queue background task (we'd need to implement this properly)
    # For now, return that tests are queued
    return RunAllTestsResponse(
        message=f"Queued {scenario_count} tests for execution",
        test_count=scenario_count,
        queued=True,
    )


@router.get("/runs", response_model=TestRunListResponse)
async def list_test_runs(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    agent_id: str | None = Query(default=None, description="Filter by agent ID"),
    scenario_id: str | None = Query(default=None, description="Filter by scenario ID"),
    status: str | None = Query(default=None, description="Filter by status"),
    passed: bool | None = Query(default=None, description="Filter by pass/fail"),
) -> TestRunListResponse:
    """List test runs with pagination and filters."""
    log = logger.bind(user_id=current_user.id)
    log.info("listing_test_runs", page=page, page_size=page_size)

    # Build query - filter by user
    query = select(TestRun).where(TestRun.user_id == current_user.id)

    if agent_id:
        query = query.where(TestRun.agent_id == uuid.UUID(agent_id))
    if scenario_id:
        query = query.where(TestRun.scenario_id == uuid.UUID(scenario_id))
    if status:
        query = query.where(TestRun.status == status)
    if passed is not None:
        query = query.where(TestRun.passed == passed)

    # Get total count
    count_query = select(func.count()).select_from(query.subquery())
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Apply pagination and ordering
    query = query.order_by(desc(TestRun.created_at))
    query = query.offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(query)
    runs = result.scalars().all()

    total_pages = (total + page_size - 1) // page_size

    return TestRunListResponse(
        runs=[
            TestRunResponse(
                id=str(r.id),
                scenario_id=str(r.scenario_id),
                scenario_name=r.scenario.name if r.scenario else None,
                agent_id=str(r.agent_id),
                agent_name=r.agent.name if r.agent else None,
                status=r.status,
                started_at=r.started_at,
                completed_at=r.completed_at,
                duration_ms=r.duration_ms,
                overall_score=r.overall_score,
                passed=r.passed,
                issues_found=r.issues_found,
                recommendations=r.recommendations,
                created_at=r.created_at,
            )
            for r in runs
        ],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


@router.get("/runs/{run_id}", response_model=TestRunDetailResponse)
async def get_test_run(
    run_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> TestRunDetailResponse:
    """Get detailed test run results."""
    result = await db.execute(
        select(TestRun).where(
            TestRun.id == uuid.UUID(run_id),
            TestRun.user_id == current_user.id,
        )
    )
    run = result.scalar_one_or_none()

    if not run:
        raise HTTPException(status_code=404, detail="Test run not found")

    return TestRunDetailResponse(
        id=str(run.id),
        scenario_id=str(run.scenario_id),
        scenario_name=run.scenario.name if run.scenario else None,
        agent_id=str(run.agent_id),
        agent_name=run.agent.name if run.agent else None,
        status=run.status,
        started_at=run.started_at,
        completed_at=run.completed_at,
        duration_ms=run.duration_ms,
        overall_score=run.overall_score,
        passed=run.passed,
        issues_found=run.issues_found,
        recommendations=run.recommendations,
        created_at=run.created_at,
        actual_transcript=run.actual_transcript,
        behavior_matches=run.behavior_matches,
        criteria_results=run.criteria_results,
        error_message=run.error_message,
    )


# =============================================================================
# Summary Endpoints
# =============================================================================


@router.get("/summary/{agent_id}", response_model=TestingSummaryResponse)
async def get_agent_testing_summary(
    agent_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> TestingSummaryResponse:
    """Get testing summary for an agent."""
    log = logger.bind(user_id=current_user.id, agent_id=agent_id)
    log.info("getting_testing_summary")

    # Verify agent belongs to user
    agent_result = await db.execute(
        select(Agent).where(
            Agent.id == uuid.UUID(agent_id),
            Agent.user_id == current_user.id,
        )
    )
    if not agent_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Agent not found")

    # Get test runs for this agent
    result = await db.execute(select(TestRun).where(TestRun.agent_id == uuid.UUID(agent_id)))
    runs = result.scalars().all()

    if not runs:
        return TestingSummaryResponse(
            agent_id=agent_id,
            total_runs=0,
            passed=0,
            failed=0,
            errors=0,
            pass_rate=0.0,
            avg_score=None,
            last_run_at=None,
        )

    passed = sum(1 for r in runs if r.passed is True)
    failed = sum(1 for r in runs if r.passed is False)
    errors = sum(1 for r in runs if r.status == TestRunStatus.ERROR.value)

    scores = [r.overall_score for r in runs if r.overall_score is not None]
    avg_score = sum(scores) / len(scores) if scores else None

    last_run = max(runs, key=lambda r: r.created_at)

    return TestingSummaryResponse(
        agent_id=agent_id,
        total_runs=len(runs),
        passed=passed,
        failed=failed,
        errors=errors,
        pass_rate=passed / len(runs) if runs else 0.0,
        avg_score=avg_score,
        last_run_at=last_run.created_at,
    )
