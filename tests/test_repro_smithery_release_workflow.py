from pathlib import Path


def test_smithery_job_packages_before_patching():
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"
    ).read_text()
    lines = workflow.splitlines()
    smithery_start = next(
        (i for i, line in enumerate(lines) if line.startswith("  smithery-publish:")),
        None,
    )
    assert smithery_start is not None, "release.yml must define a smithery-publish job"

    smithery_job_lines = []
    for line in lines[smithery_start + 1 :]:
        if line.startswith("  ") and not line.startswith("    "):
            break
        smithery_job_lines.append(line.strip())

    pack_step_name = "- name: Pack MCPB Bundle"
    patch_step_name = "- name: Build Smithery Bundle"
    assert pack_step_name in smithery_job_lines, "smithery-publish must pack Adeu.mcpb"
    assert patch_step_name in smithery_job_lines, (
        "smithery-publish must run the Smithery patch step"
    )

    pack_step_index = smithery_job_lines.index(pack_step_name)
    patch_step_index = smithery_job_lines.index(patch_step_name)

    assert pack_step_index < patch_step_index, (
        "smithery-publish must package Adeu.mcpb before patching it"
    )

    pack_step_lines = smithery_job_lines[pack_step_index:patch_step_index]
    assert any(
        "working-directory: ./desktop-extension" == line for line in pack_step_lines
    ), "Pack MCPB Bundle step must use working-directory: ./desktop-extension"
    assert any(
        line.startswith("run: zip -r") and "Adeu.mcpb" in line
        for line in pack_step_lines
    ), "Pack MCPB Bundle step must include a zip command that creates Adeu.mcpb"
