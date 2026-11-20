"""SentinEX local secure-code review interface."""

from pathlib import Path

import streamlit as st

from sentinex.ai_review import (
    AIReviewError,
    analyze_prompt,
    api_key_configured,
    findings_prompt,
    full_review_prompts,
    propose_patch,
    review_prompts,
)
from sentinex.docker_runner import DockerTestError, run_restricted_tests
from sentinex.github_pr import (
    PullRequestError,
    github_mcp_token_configured,
    publish_as_draft_pr,
)
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
                    st.session_state.pop("created_pr_result", None)
                    st.success(f"Patch applied on branch {branch}.")
                except PatchError as error:
                    st.error(f"Patch not applied: {error}")


def render_gemini_analysis(root: Path, findings: list[dict]) -> None:
    """Preview the exact bounded prompt before an explicit Gemini request."""
    st.subheader("Gemini findings review")
    included_count = min(len(findings), 20)
    st.caption(
        f"The preview below is the exact request payload for the first {included_count} finding(s), "
        "including redacted nearby code. No code is sent until you click Analyze with Gemini."
    )
    prompt = st.session_state.get("gemini_prompt")
    if not prompt:
        prompt = findings_prompt(root, findings)
        st.session_state["gemini_prompt"] = prompt
    with st.expander("Preview findings and code context to be sent"):
        st.code(prompt, language="text")

    if not api_key_configured():
        st.info("Add your Gemini API key to .env to enable findings analysis.")
        return
    if st.button("Analyze with Gemini", type="primary", key="analyze_findings"):
        try:
            with st.spinner("Gemini is analyzing the findings and nearby code..."):
                st.session_state["analysis"] = analyze_prompt(prompt)
        except AIReviewError as error:
            st.error(f"Gemini analysis failed: {error}")
        except Exception as error:
            st.error(f"Gemini analysis failed: {error}")


def render_post_patch_checks() -> None:
    """Run user-approved container tests and then rescan the patched project."""
    branch = st.session_state.get("last_patch_branch")
    root_value = st.session_state.get("last_patch_root")
    if not branch or not root_value:
        return

    st.subheader("Post-patch checks")
    st.caption(
        f"Patch branch: `{branch}`. Tests run in Docker with no network access and a "
        "read-only source mount copied into disposable container storage. The test image must "
        "already contain the project’s test tools."
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

    if result and rescanned is not None:
        render_draft_pr(branch, result.exit_code, len(rescanned))


def render_draft_pr(branch: str, test_exit_code: int, finding_count: int) -> None:
    """Offer a deliberate push-and-draft-PR action for the approved fix branch."""
    st.subheader("Create a GitHub draft PR")
    st.caption(
        "SentinEX will commit and push this approved single-file fix branch, then ask GitHub MCP "
        "to open a draft PR. It will not merge the PR."
    )
    test_status = "passed" if test_exit_code == 0 else f"failed (exit code {test_exit_code})"
    default_body = (
        "## SentinEX security fix\n\n"
        f"- Branch: `{branch}`\n"
        f"- Restricted Docker tests: {test_status}\n"
        f"- Semgrep findings after patch: {finding_count}\n\n"
        "Please review the patch and CI results before merging."
    )
    with st.form("create_draft_pr"):
        title = st.text_input("Pull request title", value="SentinEX: apply approved security fix")
        base = st.text_input("Base branch (blank uses the detected default)", value="")
        body = st.text_area("Draft pull request description", value=default_body, height=180)
        confirm = st.checkbox(
            "I reviewed the test result and Semgrep rescan, and approve pushing this branch to GitHub."
        )
        submitted = st.form_submit_button(
            "Push branch and create draft PR",
            disabled=bool(st.session_state.get("created_pr_result")),
        )

    if submitted:
        if not confirm:
            st.error("Confirm that you reviewed the checks before publishing the branch.")
            return
        try:
            with st.spinner("Pushing the approved branch and creating a draft PR through GitHub MCP..."):
                result = publish_as_draft_pr(
                    Path(st.session_state["last_patch_root"]), branch, title, body, base
                )
            st.session_state["created_pr_result"] = result
            st.success("Draft pull request created. Review and merge it on GitHub when ready.")
        except (PullRequestError, OSError, ValueError) as error:
            st.error(f"Draft PR creation failed: {error}")
        except Exception as error:
            st.error(f"Draft PR creation failed: {error}")

    if st.session_state.get("created_pr_result"):
        st.code(st.session_state["created_pr_result"])


def main() -> None:
    st.set_page_config(page_title="SentinEX", page_icon="🛡️", layout="wide")
    st.title("SentinEX")
    st.caption(
        "AI-assisted application security review with local scanning, approval-gated fixes, "
        "restricted tests, rescanning, and human-reviewed GitHub draft PRs."
    )

    with st.sidebar:
        st.header("Project")
        project_path = st.text_input(
            "Selected project folder",
            placeholder=r"E:\path\to\your\project",
        )
        st.caption("Eligible source files are read only inside this folder.")
        st.caption(
            "Semgrep runs locally when available. If Windows blocks its executable, "
            "SentinEX can run it from the official Docker image; that fallback may contact "
            "Semgrep's rule registry, with your project mounted read-only."
        )
        ai_ready = api_key_configured()
        st.divider()
        st.write(f"Gemini API key: {'configured' if ai_ready else 'not configured'}")
        github_ready = github_mcp_token_configured()
        st.write(f"GitHub MCP token: {'configured' if github_ready else 'not configured'}")
        st.caption("Full AI Review sends eligible source files to Gemini in bounded chunks.")

    scan_column, review_column = st.columns(2)
    with scan_column:
        scan_clicked = st.button(
            "Scan project",
            type="primary",
            use_container_width=True,
        )
    with review_column:
        full_review_clicked = st.button(
            "Prepare Full AI Review preview",
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
            st.session_state.pop("gemini_prompt", None)
            for key in (
                "last_patch_branch",
                "last_patch_root",
                "post_patch_result",
                "post_patch_findings",
                "created_pr_result",
                "full_review_prompts",
            ):
                st.session_state.pop(key, None)
            for key in list(st.session_state):
                if key.startswith("patch_proposal_"):
                    del st.session_state[key]

            st.success(f"Scan complete: {len(findings)} finding(s).")
            if not findings:
                st.info(
                    "Semgrep returned no findings for this ruleset. "
                    "That does not prove the project is vulnerability-free."
                )
            else:
                st.session_state["gemini_prompt"] = findings_prompt(root, findings)
        except (OSError, ValueError, ScanError, AIReviewError) as error:
            st.error(f"Scan failed: {error}")
        except Exception as error:
            st.error(f"Scan failed: {error}")

    if full_review_clicked:
        try:
            root = resolve_project_root(project_path)
            st.session_state["full_review_prompts"] = full_review_prompts(root)
            st.session_state.pop("full_review", None)
            st.session_state["project_root"] = str(root)
        except (OSError, ValueError, AIReviewError) as error:
            st.error(f"Full AI Review failed: {error}")
        except Exception as error:
            st.error(f"Full AI Review failed: {error}")

    findings = st.session_state.get("findings", [])
    if findings:
        render_gemini_analysis(Path(st.session_state["project_root"]), findings)
        render_findings(findings)
    render_post_patch_checks()
    if st.session_state.get("analysis"):
        st.subheader("Gemini analysis")
        st.markdown(st.session_state["analysis"])
    full_prompts = st.session_state.get("full_review_prompts", [])
    if full_prompts:
        st.subheader("Full AI review preview")
        st.caption(
            f"This preview contains {len(full_prompts)} bounded prompt chunk(s), with eligible "
            "source text redacted and capped. Nothing is sent until you choose the button below."
        )
        with st.expander("Preview all full-review chunks"):
            st.code("\n\n".join(full_prompts), language="text")
        if not api_key_configured():
            st.info("Add your Gemini API key to .env before sending this preview.")
        elif st.button("Send previewed full review to Gemini", key="send_full_review"):
            try:
                with st.spinner("Gemini is reviewing the previewed source chunks..."):
                    st.session_state["full_review"] = review_prompts(full_prompts)
            except AIReviewError as error:
                st.error(f"Full AI Review failed: {error}")
            except Exception as error:
                st.error(f"Full AI Review failed: {error}")
    if st.session_state.get("full_review"):
        st.subheader("Full AI Review")
        st.markdown(st.session_state["full_review"])

    st.divider()
    st.caption(
        "An approved patch changes the selected repository locally on a new Git branch. "
        "SentinEX never merges or deploys a pull request."
    )


if __name__ == "__main__":
    main()

