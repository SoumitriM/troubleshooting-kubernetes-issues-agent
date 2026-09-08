import inspect
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite
from fastapi import FastAPI, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from neo4j import Driver, GraphDatabase

from app.agent.checkpointing import strict_checkpoint_serializer
from app.agent.nodes import AgentSettings, IncidentNodes
from app.agent.workflow import build_workflow
from app.api.incidents import router as incidents_router
from app.api.knowledge import router as knowledge_router
from app.enrichment.fh_genie_client import EnrichmentSettings, FHGenieClient
from app.graph.loader import GraphSettings
from app.graph.analytics import GraphAnalyticsRepository
from app.retrieval.embeddings import IssueEmbedder
from app.retrieval.graph_retriever import GraphRetriever


LOGGER = logging.getLogger(__name__)


@dataclass
class AgentResources:
    workflow: Any
    driver: Driver


def build_agent_resources(checkpointer: Any | None = None) -> AgentResources:
    """Create process-scoped external clients and one compiled workflow."""

    graph_settings = GraphSettings()
    driver = GraphDatabase.driver(
        graph_settings.neo4j_uri,
        auth=(
            graph_settings.neo4j_username,
            graph_settings.neo4j_password.get_secret_value(),
        ),
    )
    try:
        driver.verify_connectivity()
        workflow = build_workflow(
            IncidentNodes(
                FHGenieClient(EnrichmentSettings()),
                GraphRetriever(driver, IssueEmbedder()),
                AgentSettings(),
                analytics_repository=GraphAnalyticsRepository(driver),
                enable_interrupts=checkpointer is not None,
            ),
            checkpointer=checkpointer,
        )
        return AgentResources(workflow=workflow, driver=driver)
    except Exception:
        driver.close()
        raise


def create_app(
    resource_factory: Callable[[], AgentResources] | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.incident_workflow = None
        application.state.neo4j_driver = None
        application.state.incident_checkpointer = None
        checkpoint_connection = None
        try:
            if resource_factory is None:
                checkpoint_path = Path("data/checkpoints.sqlite")
                checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
                checkpoint_connection = await aiosqlite.connect(checkpoint_path)
                checkpointer = AsyncSqliteSaver(
                    checkpoint_connection,
                    serde=strict_checkpoint_serializer(),
                )
                await checkpointer.setup()
                resources = await run_in_threadpool(
                    build_agent_resources, checkpointer
                )
                application.state.incident_checkpointer = checkpointer
            else:
                candidate = resource_factory()
                resources = (
                    await candidate if inspect.isawaitable(candidate) else candidate
                )
            application.state.incident_workflow = resources.workflow
            application.state.neo4j_driver = resources.driver
        except Exception:
            LOGGER.error("Incident analysis dependencies unavailable at startup")
        try:
            yield
        finally:
            driver = application.state.neo4j_driver
            if driver is not None:
                await run_in_threadpool(driver.close)
            if checkpoint_connection is not None:
                await checkpoint_connection.close()

    application = FastAPI(
        title="AI Incident Resolution Platform",
        lifespan=lifespan,
    )
    application.include_router(incidents_router)
    application.include_router(knowledge_router)

    @application.get("/health")
    def health() -> dict[str, str]:
        """Report whether the API process is healthy."""

        return {"status": "healthy"}

    @application.get("/ready")
    def ready() -> dict[str, str]:
        """Report whether request-serving dependencies initialized successfully."""

        if getattr(application.state, "incident_workflow", None) is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Application dependencies are not ready",
            )
        return {"status": "ready"}

    return application


app = create_app()
