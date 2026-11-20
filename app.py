"""SentinEX local secure-code review interface."""

from pathlib import Path

import streamlit as st

from sentinex.ai_review import (
    AIReviewError,
    api_key_configured,
    explain_findings,
    full_project_review,
    propose_patch,
)
from sentinex.docker_runner import DockerTestError, run_restricted_tests
from sentinex.patching import PatchError, apply_approved_patch
from sentinex.project import resolve_project_root
from sentinex.scanner import ScanError, scan_project


def render_findings(findings: list[dict]) -> None:
    """Render scanner results and approval-gated patch actions."""
    st.subheader("Semgrep findings")
    rows = []
    for finding in findings:
        extra = finding.get("extra", {})
        rows.append({
            "severity": extra.get("severity", "INFO"),
            "rule": finding.get("check_id", ""),
            "file": finding.get("path", ""),
            "line": finding.get("start", {}).get("line", ""),
            "finding": extra.get("message", ""),
        })
    st.dataframe(rows, use_container_width=True, hide_index=True)

    st.subheader("AI-proposed patches")
    st.caption(
        "Inspect each diff. SentinEX applies it only after you approve, "
        "and only to a clean Git repository on a new branch."
    )
    root = Path(st.session_state["project_root"])
    for index, finding in enumerate(findings[:5]):
        location = finding.get("start", {}).get("line", "")
        label = f"{finding.get('check_id', 'Finding')} - {finding.get('path', '')}:{location}"
        with st.expander(label):
            proposal_key = f"patch_proposal_{index}"
            if st.button(
                "Ask Gemini for a patch",
                key=f"request_patch_{index}",
                disabled=not api_key_configured(),
            ):
                try:
                    with st.spinner("Gemini is drafting a minimal diff..."):
                        st.session_state[proposal_key] = propose_patch(root, finding)
                except (AIReviewError, ValueError) as error:
                    st.error(f"Patch proposal failed: {error}")
                except Exception as error:
                    st.error(f"Patch proposal failed: {error}")

            proposal = st.session_state.get(proposal_key)
            if not proposal:
                continue
            st.write(proposal.get("summary", ""))
            diff = proposal.get("diff", "")
            if not diff:
                st.info("Gemini did not provide a patch for this finding.")
                continue

            st.code(diff, language="diff")
            if st.button(
                "Approve and apply this patch",
                key=f"approve_patch_{index}",
                type="primary",
            ):
                try:
                    branch = apply_approved_patch(root, diff)
                    st.session_state["last_patch_branch"] = branch
                    st.session_state["last_patch_root"] = str(root)
                    st.session_state.pop("post_patch_result", None)
                    st.session_state.pop("post_patch_findings", None)
                    st.success(f"Patch applied on branch {branch}.")
                except PatchError as error:
                    st.error(f"Patch not applied: {error}")


def render_post_patch_checks() -> None:
    """Run user-approved container tests and then rescan the patched project."""
    branch = st.session_state.get("last_patch_branch")
    root_value = st.session_state.get("last_patch_root")
    if not branch or not root_value:
        return

    st.subheader("Post-patch checks")
    st.caption(
        f"Patch branch: `{branch}`. Tests run in Docker with no network access and a "
        "read-only project mount. The test image must already contain the project’s test tools."
    )
    with st.form("post_patch_checks"):
        image = st.text_input("Linux Docker image", value="python:3.14-slim")
        command = st.text_input(
            "Test command to run",
            placeholder="For example: python -m pytest -q",
            max_chars=500,
        )
        submitted = st.form_submit_button("Run tests and rescan with Semgrep")

    if submitted:
        root = Path(root_value)
        try:
            with st.spinner("Running the approved command in a restricted Docker container..."):
                result = run_restricted_tests(root, image, command)
            st.session_state["post_patch_result"] = result
            with st.spinner("Rescanning the patched project with Semgrep..."):
                st.session_state["post_patch_findings"] = scan_project(root)
        except (DockerTestError, ScanError, OSError, ValueError) as error:
            st.error(f"Post-patch check failed: {error}")
        except Exception as error:
            st.error(f"Post-patch check failed: {error}")

    result = st.session_state.get("post_patch_result")
    if result:
        if result.exit_code == 0:
            st.success(f"Tests passed in {result.duration_seconds} seconds.")
        else:
            st.error(f"Tests exited with code {result.exit_code}.")
        st.code(result.output)

    rescanned = st.session_state.get("post_patch_findings")
    if rescanned is not None:
        st.write(f"Post-patch Semgrep findings: {len(rescanned)}")
        if rescanned:
            st.dataframe(
                [
                    {
                        "severity": finding.get("extra", {}).get("severity", "INFO"),
                        "rule": finding.get("check_id", ""),
                        "file": finding.get("path", ""),
                        "line": finding.get("start", {}).get("line", ""),
                        "finding": finding.get("extra", {}).get("message", ""),
                    }
                    for finding in rescanned
                ],
                use_container_width=True,
                hide_index=True,
            )


def main() -> None:
    st.set_page_config(page_title="SentinEX", page_icon="🛡️", layout="wide")
    st.title("SentinEX")
    st.caption(
        "AI-assisted application security review. Scanning and AI review are available now. "
        "Restricted tests, rescans, and GitHub pull requests are the next milestones."
    )

    with st.sidebar:
        st.header("Project")
        project_path = st.text_input(
            "Selected project folder",
            placeholder=r"E:\path\to\your\project",
        )
        st.caption("Eligible source files are read only inside this folder.")
        ai_ready = api_key_configured()
        st.divider()
        st.write(f"Gemini API key: {'configured' if ai_ready else 'not configured'}")
        st.caption("Full AI Review sends eligible source files to Gemini in bounded chunks.")

    scan_column, review_column = st.columns(2)
    with scan_column:
        scan_clicked = st.button(
            "Scan and analyze",
            type="primary",
            use_container_width=True,
        )
    with review_column:
        full_review_clicked = st.button(
            "Full AI Review",
            disabled=not ai_ready,
            use_container_width=True,
        )

    if scan_clicked:
        try:
            root = resolve_project_root(project_path)
            with st.spinner("Scanning the selected project with Semgrep..."):
                findings = scan_project(root)
            st.session_state["project_root"] = str(root)
            st.session_state["findings"] = findings
            st.session_state.pop("analysis", None)
            st.session_state.pop("full_review", None)
            for key in list(st.session_state):
                if key.startswith("patch_proposal_"):
                    del st.session_state[key]

            st.success(f"Scan complete: {len(findings)} finding(s).")
            if findings and ai_ready:
                with st.spinner("Sending findings and nearby code context to Gemini..."):
                    st.session_state["analysis"] = explain_findings(root, findings)
            elif findings:
                st.info("Add a Gemini API key to enable AI analysis.")
            else:
                st.info(
                    "Semgrep returned no findings for this ruleset. "
                    "That does not prove the project is vulnerability-free."
                )
        except (OSError, ValueError, ScanError, AIReviewError) as error:
            st.error(f"Scan failed: {error}")
        except Exception as error:
            st.error(f"Scan failed: {error}")

    if full_review_clicked:
        try:
            root = resolve_project_root(project_path)
            with st.spinner("Sending eligible source files to Gemini in bounded chunks..."):
                st.session_state["full_review"] = full_project_review(root)
            st.session_state["project_root"] = str(root)
        except (OSError, ValueError, AIReviewError) as error:
            st.error(f"Full AI Review failed: {error}")
        except Exception as error:
            st.error(f"Full AI Review failed: {error}")

    findings = st.session_state.get("findings", [])
    if findings:
        render_findings(findings)
    render_post_patch_checks()
    if st.session_state.get("analysis"):
        st.subheader("Gemini analysis")
        st.markdown(st.session_state["analysis"])
    if st.session_state.get("full_review"):
        st.subheader("Full AI Review")
        st.markdown(st.session_state["full_review"])

    st.divider()
    st.caption(
        "An approved patch changes the selected repository locally on a new Git branch. "
        "Docker test isolation and approval-gated PR creation are not implemented yet."
    )


if __name__ == "__main__":
    main()

