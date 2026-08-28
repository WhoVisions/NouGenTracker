"""Regression guards for the public Hugging Face tracker snapshot."""

from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "deploy-space.yml"


def test_space_snapshot_carries_connector_data_tree():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "cp -R dailies/. hf-space/dailies/" in workflow
    assert "Refusing to deploy a tracker snapshot without public dailies" in workflow
    assert '"data_root": "dailies"' in workflow


def test_space_verification_checks_dailies_tree_not_only_dashboard():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "/tree/main/dailies" in workflow
    assert "Dashboard and dailies tree are serving the new deploy" in workflow
