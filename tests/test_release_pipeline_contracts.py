from __future__ import annotations

import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_ROOT = PROJECT_ROOT / ".github" / "workflows"
FULL_SHA_ACTION = re.compile(r"^\s*uses:\s*[^@\s]+@([0-9a-f]{40})(?:\s+#.*)?$", re.MULTILINE)
ANY_ACTION = re.compile(r"^\s*uses:\s*[^@\s]+@([^\s]+)", re.MULTILINE)


def test_every_third_party_action_is_pinned_to_full_commit_sha():
    violations: list[str] = []
    for path in sorted(WORKFLOW_ROOT.glob("*.yml")):
        source = path.read_text(encoding="utf-8")
        for match in ANY_ACTION.finditer(source):
            if not re.fullmatch(r"[0-9a-f]{40}", match.group(1)):
                violations.append(f"{path.name}: {match.group(0).strip()}")
    assert violations == []


def test_release_pipeline_never_repairs_dirty_source_by_deleting_evidence():
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [
            PROJECT_ROOT / "build" / "build_windows_x64.ps1",
            *sorted(WORKFLOW_ROOT.glob("*.yml")),
        ]
    ).lower()
    forbidden = (
        "git checkout --",
        "git clean ",
        "git reset --hard",
        "reset-item",
    )
    assert all(token not in sources for token in forbidden)


def test_release_generation_is_redirected_out_of_tracked_paths():
    build = (PROJECT_ROOT / "build" / "build_windows_x64.ps1").read_text(
        encoding="utf-8-sig"
    )
    assert "--destination $InstructionAssets" in build
    assert "tools\\generate_manifest.py --output $SourceManifest" in build
    assert "generate_windows_version_info.py --output $VersionInfo" in build
    assert "$StageRoot = Join-Path $OutputRoot \"staging\"" in build
    assert build.count('Assert-CleanCheckout "') >= 4

    spec = (PROJECT_ROOT / "build" / "LansetSpBot.windows.spec").read_text(
        encoding="utf-8"
    )
    assert "LANSETSPBOT_BUILD_INSTRUCTION_ASSETS" in spec
    assert "LANSETSPBOT_BUILD_VERSION_INFO" in spec

    capture = (
        PROJECT_ROOT / "tools" / "capture_instruction_screenshots.py"
    ).read_text(encoding="utf-8")
    assert 'DESTINATION = PROJECT_ROOT / "dist" / "instruction-assets"' in capture
    assert 'os.environ["LANSETSPBOT_DATA_DIR"] = str(profile)' in capture
    assert 'os.environ["MARLEN_DATA_DIR"] = str(profile)' in capture


def test_windows_ci_has_313_release_and_314_source_proofs():
    workflow = (WORKFLOW_ROOT / "windows-release-proof.yml").read_text(
        encoding="utf-8"
    )
    assert 'python-version: "3.13"' in workflow
    assert 'python-version: "3.14"' in workflow
    assert "prove-python-314-source:" in workflow
    build = (PROJECT_ROOT / "build" / "build_windows_x64.ps1").read_text(
        encoding="utf-8-sig"
    )
    assert 'Join-Path $ProofRoot "release-proof.json"' in build


def test_dependency_audit_covers_all_lock_files_and_uploads_json():
    workflow = (WORKFLOW_ROOT / "dependency-audit.yml").read_text(encoding="utf-8")
    for name in (
        "requirements-runtime.lock",
        "requirements-openai.lock",
        "requirements-build-windows-x64.lock",
        "requirements-dev-windows-x64.lock",
    ):
        assert name in workflow
    assert "--format=json" in workflow
    assert "dependency-audit.json" in workflow
    assert "disable-pip: true" in workflow
    assert "enforce_dependency_audit_policy.py" in workflow


def test_authenticode_and_attestation_are_release_only():
    workflow = (WORKFLOW_ROOT / "windows-release-sign-attest.yml").read_text(
        encoding="utf-8"
    )
    assert 'tags:\n      - "v*"' in workflow
    assert "pull_request:" not in workflow
    assert "environment: release-signing" in workflow
    assert "WINDOWS_SIGNING_PFX_B64" in workflow
    assert "WINDOWS_SIGNING_PFX_PASSWORD" in workflow
    assert "actions/attest-build-provenance@" in workflow

    build = (PROJECT_ROOT / "build" / "build_windows_x64.ps1").read_text(
        encoding="utf-8-sig"
    )
    assert 'if ($env:GITHUB_EVENT_NAME -eq "pull_request")' in build
    assert "Import-PfxCertificate" in build
    assert "signtool.exe" in build
    assert "authenticode_signed = $ReleaseSigned" in build


def test_failure_evidence_does_not_upload_the_entire_dist_tree():
    workflow = (WORKFLOW_ROOT / "windows-release-proof.yml").read_text(
        encoding="utf-8"
    )
    failure_section = workflow.split("- name: Upload failure evidence", 1)[1]
    failure_section = failure_section.split("prove-python-314-source:", 1)[0]
    assert "dist/**" not in failure_section
    assert "dist/ci-proof/**" in failure_section



def test_release_build_uses_external_process_watchdog():
    workflow = (WORKFLOW_ROOT / "windows-release-proof.yml").read_text(
        encoding="utf-8"
    )
    section = workflow.split(
        "- name: Run complete Windows release pipeline", 1
    )[1].split("- name: Verify release evidence", 1)[0]

    assert "python tools/run_ci_subprocess.py" in section
    assert "--label windows-release" in section
    assert '--log "dist\\ci-proof\\build.log"' in section
    assert "--idle-timeout-seconds 900" in section
    assert "--total-timeout-seconds 6600" in section
    assert "Tee-Object" not in section
