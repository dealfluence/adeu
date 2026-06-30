from pathlib import Path


def test_smithery_publish_job_packs_mcpb_before_patching():
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"
    ).read_text()

    smithery_job = workflow.split("  smithery-publish:\n", 1)[1].split(
        "\n  pypi-publish:", 1
    )[0]
    normalized_lines = [line.strip() for line in smithery_job.splitlines()]

    assert "- name: Pack MCPB Bundle" in normalized_lines
    assert (
        "run: zip -r Adeu.mcpb manifest.json icon.png package.json *.js assets/ templates/"
        in normalized_lines
    )
    assert smithery_job.index("Pack MCPB Bundle") < smithery_job.index(
        "Build Smithery Bundle"
    )
