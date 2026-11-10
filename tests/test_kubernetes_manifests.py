from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
K8S = ROOT / "k8s"


def manifest(name: str) -> str:
    return (K8S / name).read_text(encoding="utf-8")


def test_app_deployment_is_single_replica_local_and_non_root() -> None:
    deployment = manifest("07-app-deployment.yaml")

    assert "replicas: 1" in deployment
    assert "type: Recreate" in deployment
    assert "image: incident-resolution-ai-app:latest" in deployment
    assert "imagePullPolicy: Never" in deployment
    assert "enableServiceLinks: false" in deployment
    assert "runAsNonRoot: true" in deployment
    assert "runAsUser: 1000" in deployment
    assert deployment.count("path: /ready") == 1
    assert deployment.count("path: /health") == 1
    assert "claimName: langgraph-checkpoints" in deployment


def test_neo4j_and_checkpoint_storage_are_persistent() -> None:
    statefulset = manifest("05-neo4j-statefulset.yaml")
    claims = manifest("03-persistent-volume-claims.yaml")
    config = manifest("01-configmap.yaml")

    assert "kind: StatefulSet" in statefulset
    assert "enableServiceLinks: false" in statefulset
    assert "claimName: neo4j-data" in statefulset
    assert "name: neo4j-data" in claims
    assert "name: langgraph-checkpoints" in claims
    assert "neo4j://neo4j:7687" in config


def test_secret_template_contains_only_explicit_placeholders() -> None:
    secret = manifest("02-secret.example.yaml")
    kustomization = manifest("kustomization.yaml")

    assert "replace-with-anthropic-api-key" in secret
    assert "replace-with-neo4j-username" in secret
    assert "replace-with-neo4j-password" in secret
    assert "02-secret.example.yaml" not in kustomization
    assert "kind: Secret" in secret


def test_frontend_is_exposed_and_proxies_to_the_internal_api() -> None:
    deployment = manifest("09-frontend-deployment.yaml")
    service = manifest("08-frontend-service.yaml")
    kustomization = manifest("kustomization.yaml")
    nginx = (ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")

    assert "image: incident-resolution-ai-frontend:latest" in deployment
    assert "imagePullPolicy: Never" in deployment
    assert "runAsNonRoot: true" in deployment
    assert "readOnlyRootFilesystem: true" in deployment
    assert deployment.count("path: /") == 2
    assert "type: LoadBalancer" in service
    assert "port: 80" in service
    assert "08-frontend-service.yaml" in kustomization
    assert "09-frontend-deployment.yaml" in kustomization
    assert "proxy_pass http://incident-resolution-ai:8000" in nginx
    assert "location = /ready" in nginx


def test_frontend_image_renders_nginx_config_into_a_separate_file() -> None:
    """The build swaps the API upstream with sed. Redirecting the output onto the
    file sed is reading empties it, and nginx then starts with no config."""

    dockerfile = (ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")
    nginx = (ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")

    assert "> /tmp/nginx.conf" in dockerfile
    assert "COPY --from=builder /tmp/nginx.conf /etc/nginx/nginx.conf" in dockerfile
    assert "nginx.conf > /build/nginx.conf" not in dockerfile
    # The default upstream the sed expression replaces must exist in the config.
    assert "http://incident-resolution-ai:8000" in nginx
