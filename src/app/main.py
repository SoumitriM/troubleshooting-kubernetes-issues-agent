import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from neo4j import Driver, GraphDatabase

from app.agent.nodes import AgentSettings, IncidentNodes
from app.agent.workflow import build_workflow
from app.api.incidents import router as incidents_router
from app.enrichment.fh_genie_client import EnrichmentSettings, FHGenieClient
from app.graph.loader import GraphSettings
from app.retrieval.embeddings import IssueEmbedder
from app.retrieval.graph_retriever import GraphRetriever


LOGGER = logging.getLogger(__name__)


@dataclass
class AgentResources:
    workflow: Any
    driver: Driver


def build_agent_resources() -> AgentResources:
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
            )
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
        factory = resource_factory or build_agent_resources
        try:
            resources = await run_in_threadpool(factory)
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

    application = FastAPI(
        title="AI Incident Resolution Platform",
        lifespan=lifespan,
    )
    application.include_router(incidents_router)

    @application.get("/health")
    def health() -> dict[str, str]:
        """Report whether the API process is healthy."""

        return {"status": "healthy"}

    return application


app = create_app()
