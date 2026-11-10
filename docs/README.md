# Documentation

This folder documents how the AI Incident Resolution Platform works and how to
run, configure and test it. The top-level [README](../README.md) remains the
reference for each data-pipeline command and for Docker Compose and Kubernetes
deployment.

| Document | Read it to |
|---|---|
| [Architecture](architecture.md) | Understand the components, the request workflow and the safety model |
| [Local development](local-development.md) | Run Neo4j, the API and the UI on your machine without Docker |
| [Configuration](configuration.md) | Look up every environment variable and its default |
| [LLM integration](llm-integration.md) | See how Claude is called, validated and swapped between backends |
| [HTTP API](api.md) | Call the API directly, including clarification threads |
| [Evaluation](evaluation.md) | Run and extend the 30-query golden dataset |
| [Project overview deck](KubeSage-project-overview.pptx) | Present what the project does, how it works, worked examples and evaluation results |
| [Docker storage recovery](DOCKER_STORAGE_RECOVERY.md) | Recover Docker Desktop when its disk fills up |
