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
from neo4j.exceptions import ServiceUnavailable

from app.agent.checkpointing import strict_checkpoint_serializer
from app.agent.nodes import AgentSettings, IncidentNodes
from app.agent.workflow import build_workflow
from app.api.chat import router as chat_router
from app.api.incidents import router as incidents_router
from app.api.knowledge import router as knowledge_router
from app.chat.store import ConversationStore
from app.enrichment.claude_client import ClaudeClient, ClaudeSettings
from app.graph.loader import GraphSettings
from app.graph.analytics import GraphAnalyticsRepository
from app.retrieval.embeddings import IssueEmbedder
from app.retrieval.graph_retriever import GraphRetriever
from app.documentation.store import DocumentationStore, KubernetesDocsRetriever


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
        # Keep an unreachable database within the request budget instead of the
        # driver's default 30 s managed-transaction retry window plus back-off.
        connection_timeout=10.0,
        max_transaction_retry_time=5.0,
    )
    try:
        try:
            driver.verify_connectivity()
        except ServiceUnavailable:
            # Neo4j may still be starting (Compose restarts, Kubernetes pods start
            # in parallel). The driver connects lazily, so graph-backed requests
            # recover once it is reachable instead of leaving the API never ready.
            LOGGER.warning("Neo4j is not reachable yet; graph routes will retry")
        embedder = IssueEmbedder()
        agent_settings = AgentSettings()
        documentation_store = DocumentationStore(
            agent_settings.documentation_index_path,
            embedder,
        )
        try:
            documentation_store.preload()
        except Exception:
            LOGGER.exception("Documentation index could not be preloaded")
        workflow = build_workflow(
            IncidentNodes(
                ClaudeClient(ClaudeSettings()),
                GraphRetriever(driver, embedder),
                agent_settings,
                analytics_repository=GraphAnalyticsRepository(driver),
                documentation_retriever=KubernetesDocsRetriever(
                    documentation_store
                ),
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
        application.state.conversation_store = None
        checkpoint_connection = None
        chat_connection = None
        try:
            # Injected resources (tests) get a throwaway in-memory chat store.
            chat_path = ":memory:"
            if resource_factory is None:
                Path("data").mkdir(exist_ok=True)
                chat_path = "data/conversations.sqlite"
            chat_connection = await aiosqlite.connect(chat_path)
            conversation_store = ConversationStore(chat_connection)
            await conversation_store.setup()
            application.state.conversation_store = conversation_store
        except Exception:
            LOGGER.exception("Chat conversation store unavailable at startup")
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
            LOGGER.exception("Incident analysis dependencies unavailable at startup")
        try:
            yield
        finally:
            driver = application.state.neo4j_driver
            if driver is not None:
                await run_in_threadpool(driver.close)
            if checkpoint_connection is not None:
                await checkpoint_connection.close()
            if chat_connection is not None:
                await chat_connection.close()

    application = FastAPI(
        title="AI Incident Resolution Platform",
        lifespan=lifespan,
    )
    application.include_router(incidents_router)
    application.include_router(knowledge_router)
    application.include_router(chat_router)

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
