from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.agent.state import QueryAnalysis, QueryUnderstanding


def strict_checkpoint_serializer() -> JsonPlusSerializer:
    """Allow safe built-ins plus the sole application type stored in state."""

    return JsonPlusSerializer(
        pickle_fallback=False,
        allowed_msgpack_modules=(QueryAnalysis, QueryUnderstanding),
    )
