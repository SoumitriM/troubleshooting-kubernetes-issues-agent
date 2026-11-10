# API image for Azure Container Apps.
#
# Extends the repository's API image with what a scale-to-zero container needs
# at startup: the Claude Code CLI (LLM_BACKEND=claude_code, authenticated by the
# CLAUDE_CODE_OAUTH_TOKEN secret), the embedding model, and the official
# Kubernetes documentation index. Build it with a named context that holds the
# index:
#
#   docker build -f deploy/azure/api.Dockerfile \
#     --build-arg BASE_IMAGE=<api image built from ./Dockerfile> \
#     --build-context docs=./data .
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

USER root
RUN apt-get update \
    && apt-get install --no-install-recommends --yes curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
USER appuser

# Pinned: the client's model and flags need 2.1.280 or newer.
ARG CLAUDE_CODE_VERSION=2.1.317
RUN curl -fsSL https://claude.ai/install.sh | bash -s "${CLAUDE_CODE_VERSION}" \
    && /home/appuser/.local/bin/claude --version

ENV PATH=/home/appuser/.local/bin:${PATH} \
    HF_HOME=/app/.cache/huggingface \
    LLM_BACKEND=claude_code \
    LLM_CLAUDE_CODE_PATH=/home/appuser/.local/bin/claude \
    DISABLE_AUTOUPDATER=1

# Download the embedding model at build time instead of on every cold start.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')"

COPY --from=docs --chown=appuser:appuser kubernetes_docs.sqlite /app/data/kubernetes_docs.sqlite
