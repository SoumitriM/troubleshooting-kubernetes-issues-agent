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
    assert deployment.count("path: /health") == 2
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

    assert "replace-with-fh-genie-key" in secret
    assert "replace-with-neo4j-username" in secret
    assert "replace-with-neo4j-password" in secret
    assert "02-secret.example.yaml" not in kustomization
    assert "kind: Secret" in secret
