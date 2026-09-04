"""Regression guards for the public Hugging Face tracker snapshot."""

from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "deploy-space.yml"


def test_space_snapshot_carries_connector_data_tree():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "find dailies -mindepth 2 -maxdepth 2" in workflow
    assert "cp -R dailies/. hf-space/dailies/" not in workflow
    assert "Refusing to deploy a tracker snapshot without public dailies" in workflow
    assert '"data_root": "dailies"' in workflow
    assert "python3 public_surface.py" in workflow


def test_space_verification_checks_dailies_tree_not_only_dashboard():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "/tree/main/dailies" in workflow
    assert "Dashboard and dailies tree are serving the new deploy" in workflow


def test_space_deploy_waits_for_green_ci_and_uses_tested_sha():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert 'workflows: ["CI"]' in workflow
    assert "github.event.workflow_run.conclusion == 'success'" in workflow
    assert "github.event.workflow_run.head_sha" in workflow
    assert "workflow_dispatch" not in workflow
    assert "${SOURCE_SHA}" in workflow
    assert "GITHUB_SHA" not in workflow
