"""Safe data-access boundary for graph-analytics queries."""

from typing import Any

from neo4j import Driver


class GraphAnalyticsRepository:
    """Execute already-validated read queries in a read transaction."""

    def __init__(self, driver: Driver) -> None:
        self._driver = driver

    def execute_read(
        self, cypher: str, parameters: dict[str, Any]
    ) -> list[dict[str, Any]]:
        with self._driver.session() as session:
            return session.execute_read(
                lambda transaction: [
                    dict(record) for record in transaction.run(cypher, **parameters)
                ]
            )
