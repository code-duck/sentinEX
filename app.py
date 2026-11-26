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
from sentinex.folder_picker import FolderPickerError, pick_project_folder
from sentinex.github_pr import (
    PullRequestError,
    github_mcp_token_configured,
    merge_approved_pull_request,
    publish_as_draft_pr,
)
from sentinex.patching import (
    PatchError,
    apply_approved_patch,
    detect_pending_patch,
    validate_patch,
)
from sentinex.project import resolve_project_root
from sentinex.rule_fixes import propose_rule_based_patch
from sentinex.scanner import ScanError, scan_project
from sentinex.test_discovery import detect_test_setup


def render_deploy_tooltip() -> None:
    """Explain Streamlit's built-in Deploy control on hover."""
    st.markdown(
        """
        <style>
        [data-testid="stDeployButton"] {
            position: relative;
        }
        [data-testid="stDeployButton"]:hover::after {
            content: "Opens Streamlit Community Cloud publishing setup for this app. It does not create a SentinEX pull request or publish the selected project.";
            position: absolute;
            top: calc(100% + 0.5rem);
            right: 0;
            z-index: 10000;
            width: 18rem;
            padding: 0.7rem 0.85rem;
            border: 1px solid rgba(250, 250, 250, 0.2);
            border-radius: 0.5rem;
            background: #262730;
            color: #fafafa;
            box-shadow: 0 0.25rem 0.8rem rgba(0, 0, 0, 0.35);
            font-size: 0.8rem;
            font-weight: 400;
            line-height: 1.4;
            white-space: normal;
            pointer-events: none;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def browse_project_folder() -> None:
    """Let the developer choose a project folder with the Windows picker."""
    try:
        selected = pick_project_folder(st.session_state.get("project_path", ""))
    except FolderPickerError as error:
        st.session_state["folder_picker_error"] = str(error)
        return

    st.session_state.pop("folder_picker_error", None)
    if selected:
        st.session_state["project_path"] = selected


def render_findings(findings: list[dict]) -> None:
    """Render scanner results and approval-gated patch actions."""
    workflow_locked = bool(
        st.session_state.get("last_patch_branch")
        and not st.session_state.get("merge_result")
    )
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

    st.subheader("Proposed patches")
    st.caption(
        "Inspect each diff and its source. SentinEX applies it only after you approve, "
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
                disabled=not api_key_configured() or workflow_locked,
                help=(
                    "Ask Gemini to suggest a fix. Finish the current patch's PR workflow "
                    "before starting another patch."
                    if workflow_locked
                    else "Ask Gemini to suggest a fix for this finding. It will not change your files."
                ),
            ):
                try:
                    with st.spinner("Gemini is drafting a minimal diff..."):
                        st.session_state[proposal_key] = propose_patch(root, finding)
                except (AIReviewError, ValueError) as error:
                    st.error(f"Patch proposal failed: {error}")
                except Exception as error:
                    st.error(f"Patch proposal failed: {error}")

            rule_patch = propose_rule_based_patch(root, finding)
            if rule_patch is not None and st.button(
                "Prepare built-in rule fix",
                key=f"rule_fix_{index}",
                disabled=workflow_locked,
                help=(
                    "Prepare a narrowly scoped local fix for this recognized Semgrep rule. "
                    "It is not AI-generated and will not change files until you approve it."
                ),
            ):
                st.session_state[proposal_key] = rule_patch

            proposal = st.session_state.get(proposal_key)
            if not proposal:
                continue
            if proposal.get("source"):
                st.info(proposal["source"])
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
                disabled=workflow_locked,
                help="Apply the diff you reviewed to this project on a new Git branch.",
            ):
                try:
                    patch_file = validate_patch(root, diff)
                    branch = apply_approved_patch(root, diff)
                    st.session_state["last_patch_branch"] = branch
                    st.session_state["last_patch_root"] = str(root)
                    st.session_state["last_patch_file"] = patch_file
                    st.session_state.pop("post_patch_result", None)
                    st.session_state.pop("post_patch_findings", None)
                    st.session_state.pop("post_patch_scan_report", None)
                    st.session_state.pop("created_pr_result", None)
                    st.session_state.pop("merge_result", None)
                    st.session_state.pop("findings", None)
                    st.session_state.pop("analysis", None)
                    st.session_state.pop("gemini_prompt", None)
                    for key in list(st.session_state):
                        if key.startswith("patch_proposal_"):
                            del st.session_state[key]
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
    st.caption("Open the preview to see the finding and redacted code context before sharing it with Gemini.")
    with st.expander("Preview findings and code context to be sent"):
        st.code(prompt, language="text")

    if not api_key_configured():
        st.info("Add your Gemini API key to .env to enable findings analysis.")
        return
    if st.button(
        "Analyze with Gemini",
        type="primary",
        key="analyze_findings",
        help="Send the previewed finding and nearby code to Gemini for an explanation.",
    ):
        try:
            with st.spinner("Gemini is analyzing the findings and nearby code..."):
                st.session_state["analysis"] = analyze_prompt(prompt)
        except AIReviewError as error:
            st.error(f"Gemini analysis failed: {error}")
        except Exception as error:
            st.error(f"Gemini analysis failed: {error}")


def render_post_patch_checks() -> None:
    """Create a draft PR, then test and rescan before offering an approved merge."""
    branch = st.session_state.get("last_patch_branch")
    root_value = st.session_state.get("last_patch_root")
    if not branch or not root_value:
        return

    render_draft_pr(branch)
    pr_info = st.session_state.get("created_pr_result")
    if not pr_info:
        return

    st.subheader("Post-patch checks")
    root = Path(root_value)
    target_file = st.session_state.get("last_patch_file")
    setup = detect_test_setup(root, target_file)
    if setup:
        st.success(f"SentinEX detected: {setup.label}")
        st.caption(setup.reason)
        if setup.dependency_note:
            st.warning(setup.dependency_note)
        image_default = setup.image
        command_default = setup.command
    else:
        st.warning(
            "SentinEX couldn't identify a common test setup for this project. "
            "You can enter the test image and command in Advanced test settings."
        )
        image_default = ""
        command_default = ""

    image_key = f"test_image_{branch}"
    command_key = f"test_command_{branch}"
    st.session_state.setdefault(image_key, image_default)
    st.session_state.setdefault(command_key, command_default)
    image = st.session_state[image_key]
    command = st.session_state[command_key]
    st.markdown("**SentinEX will run this in Docker:**")
    st.code(f"Image: {image or '(choose an image)'}\nCommand: {command or '(no test command detected)'}")
    st.caption(
        "The selected project is copied into a restricted container with network access disabled. "
        "SentinEX does not install project dependencies."
    )

    with st.expander("Advanced test settings", expanded=not bool(setup)):
        st.text_input(
            "Docker test image",
            key=image_key,
            placeholder="For example: python:3.14-slim",
            help="Choose an image that already contains the project’s test tools and dependencies.",
        )
        st.text_input(
            "Test command",
            key=command_key,
            placeholder="For example: python -m unittest discover -v",
            max_chars=500,
            help="Edit the detected command or enter one if SentinEX could not detect tests.",
        )

    image = st.session_state[image_key].strip()
    command = st.session_state[command_key].strip()
    approval_hash = f"{image}\0{command}"
    confirm = st.checkbox(
        "I approve running this test command on the disposable project copy in Docker.",
        key=f"approve_tests_{branch}_{abs(hash(approval_hash))}",
        disabled=not image or not command,
        help="Review the Docker image and command above before approving the run.",
    )
    submitted = st.button(
        "Run project tests and rescan with Semgrep",
        disabled=not (confirm and image and command),
        help="Run the displayed command in Docker, then rescan the patched project with Semgrep.",
    )

    if submitted:
        root = Path(root_value)
        for key in (
            "post_patch_result",
            "post_patch_findings",
            "post_patch_scan_report",
            "merge_result",
        ):
            st.session_state.pop(key, None)
        try:
            with st.spinner("Running the approved command in a restricted Docker container..."):
                result = run_restricted_tests(root, image, command)
            st.session_state["post_patch_result"] = result
            with st.spinner("Rescanning the patched project with Semgrep..."):
                scan_report = scan_project(root)
                st.session_state["post_patch_findings"] = scan_report.findings
                st.session_state["post_patch_scan_report"] = {
                    "files": len(scan_report.scanned_files),
                    "engine": scan_report.engine,
                    "skipped": scan_report.skipped_files,
                }
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
        scan_meta = st.session_state.get("post_patch_scan_report", {})
        st.write(
            f"Post-patch Semgrep findings: {len(rescanned)} "
            f"({scan_meta.get('files', 0)} files scanned with {scan_meta.get('engine', 'Semgrep')})"
        )
        if scan_meta.get("skipped"):
            st.warning(f"Semgrep skipped {len(scan_meta['skipped'])} file(s); review coverage before merging.")
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
        render_merge_approval(Path(root_value), pr_info, result, rescanned)


def render_draft_pr(branch: str) -> None:
    """Offer a deliberate push-and-draft-PR action for the approved fix branch."""
    st.subheader("Create a GitHub draft PR")
    st.caption(
        "SentinEX will commit and push this approved single-file fix branch, then ask GitHub MCP "
        "to open a draft PR. You can run local checks afterward and explicitly approve any merge."
    )
    default_body = (
        "## SentinEX security fix\n\n"
        f"- Branch: `{branch}`\n"
        "- Restricted Docker tests: pending\n"
        "- Semgrep rescan: pending\n\n"
        "Please review the patch and checks before merging."
    )
    with st.form("create_draft_pr"):
        title = st.text_input(
            "Pull request title",
            value="SentinEX: apply approved security fix",
            help="Choose the short title developers will see on GitHub.",
        )
        base = st.text_input(
            "Base branch (blank uses the detected default)",
            value="",
            help="Choose the branch to compare against, usually main. Leave blank to detect it.",
        )
        body = st.text_area(
            "Draft pull request description",
            value=default_body,
            height=180,
            help="Review or edit the summary that will appear in the GitHub draft PR.",
        )
        confirm = st.checkbox(
            "I reviewed this patch and approve pushing its branch to GitHub.",
            help="SentinEX will only push and open the draft PR after you check this box and submit.",
        )
        submitted = st.form_submit_button(
            "Push branch and create draft PR",
            disabled=bool(st.session_state.get("created_pr_result")),
            help="Push the approved fix branch and open a draft PR for developer review.",
        )

    if submitted:
        if not confirm:
            st.error("Confirm that you reviewed the patch before publishing the branch.")
            return
        try:
            with st.spinner("Pushing the approved branch and creating a draft PR through GitHub MCP..."):
                result = publish_as_draft_pr(
                    Path(st.session_state["last_patch_root"]), branch, title, body, base
                )
            st.session_state["created_pr_result"] = result
            st.success("Draft pull request created. Run the local checks below before deciding whether to merge.")
        except (PullRequestError, OSError, ValueError) as error:
            st.error(f"Draft PR creation failed: {error}")
        except Exception as error:
            st.error(f"Draft PR creation failed: {error}")

    if st.session_state.get("created_pr_result"):
        pr_info = st.session_state["created_pr_result"]
        st.link_button(
            f"Open draft pull request #{pr_info.number}",
            pr_info.url,
            help="Open the draft PR on GitHub to inspect the proposed change.",
        )


def render_merge_approval(root: Path, pr_info, result, findings: list[dict]) -> None:
    """Ask for explicit human approval before requesting a GitHub merge."""
    st.subheader("Review checks and merge")
    if result.exit_code != 0:
        st.info("The Docker tests did not pass, so SentinEX will not offer a merge action.")
        return

    scan_meta = st.session_state.get("post_patch_scan_report", {})
    if scan_meta.get("skipped"):
        st.error(
            "Semgrep skipped source files, so SentinEX will not offer a merge action. "
            "Resolve the scan coverage issue and run the checks again."
        )
        return

    if findings:
        acknowledge_findings = st.checkbox(
            f"I reviewed the rescan and accept that {len(findings)} Semgrep finding(s) remain.",
            key="acknowledge_remaining_findings",
            help="Review the remaining findings above before deciding whether to merge the PR.",
        )
    else:
        acknowledge_findings = True
        st.success("The rescan found no remaining Semgrep findings.")

    confirm = st.checkbox(
        f"I reviewed PR #{pr_info.number}, the Docker test output, and the Semgrep rescan; merge this PR.",
        key="approve_pr_merge",
        help="This is the final human approval. Checking it alone does not merge; submit the button below.",
    )
    if st.button(
        "Merge pull request",
        type="primary",
        disabled=not (confirm and acknowledge_findings) or bool(st.session_state.get("merge_result")),
        key="merge_pull_request",
        help="Ask GitHub to squash-merge this PR after your explicit approval.",
    ):
        try:
            with st.spinner(f"Merging pull request #{pr_info.number} through GitHub MCP..."):
                st.session_state["merge_result"] = merge_approved_pull_request(root, pr_info.number)
            st.success("Pull request merged.")
        except (PullRequestError, OSError, ValueError) as error:
            st.error(f"Pull request merge failed: {error}")
        except Exception as error:
            st.error(f"Pull request merge failed: {error}")
    if st.session_state.get("merge_result"):
        st.write(st.session_state["merge_result"])


def main() -> None:
    st.set_page_config(page_title="SentinEX", page_icon="🛡️", layout="wide")
    render_deploy_tooltip()
    st.title("SentinEX")
    st.caption(
        "AI-assisted application security review with local scanning, approval-gated fixes, "
        "restricted tests, rescanning, and human-reviewed GitHub draft PRs."
    )

    with st.sidebar:
        st.header("Project")
        st.session_state.setdefault("project_path", "")
        st.button(
            "Browse for project folder",
            on_click=browse_project_folder,
            use_container_width=True,
            help="Open File Explorer and choose the project folder SentinEX may inspect.",
        )
        if st.session_state.get("folder_picker_error"):
            st.error(st.session_state["folder_picker_error"])
        project_path = st.text_input(
            "Selected project folder",
            placeholder=r"E:\path\to\your\project",
            key="project_path",
            help="The folder SentinEX will scan. Eligible files outside it are not included.",
        )
        st.caption("Eligible source files are read only inside this folder.")
        st.caption(
            "On Windows, Semgrep runs in a resource-limited Docker container. The selected "
            "project is mounted read-only; Semgrep may contact its rule registry. SentinEX "
            "also runs its included local security rules."
        )
        ai_ready = api_key_configured()
        st.divider()
        st.write(f"Gemini API key: {'configured' if ai_ready else 'not configured'}")
        github_ready = github_mcp_token_configured()
        st.write(f"GitHub MCP token: {'configured' if github_ready else 'not configured'}")
        st.caption("Full AI Review sends eligible source files to Gemini in bounded chunks.")

    pending_patch = None
    if project_path and not st.session_state.get("last_patch_branch"):
        try:
            selected_root = resolve_project_root(project_path)
            pending_patch = detect_pending_patch(selected_root)
        except (OSError, ValueError):
            pending_patch = None
    if pending_patch:
        pending_branch, pending_file = pending_patch
        st.warning(
            f"Found an uncommitted change to `{pending_file}` on `{pending_branch}`. "
            "This looks like a patch SentinEX already applied. Resume its PR workflow "
            "instead of applying another patch."
        )
        if st.button(
            "Resume this patch's PR workflow",
            key="resume_pending_patch",
            help="Restore the current SentinEX branch in the dashboard; this does not commit or push it.",
        ):
            selected_root = resolve_project_root(project_path)
            st.session_state["last_patch_branch"] = pending_branch
            st.session_state["last_patch_root"] = str(selected_root)
            st.session_state["last_patch_file"] = pending_file
            for key in (
                "findings",
                "scan_report",
                "scan_error",
                "analysis",
                "gemini_prompt",
                "full_review",
                "full_review_prompts",
            ):
                st.session_state.pop(key, None)
            for key in list(st.session_state):
                if key.startswith("patch_proposal_"):
                    del st.session_state[key]
            st.rerun()

    workflow_locked = bool(pending_patch) or bool(
        st.session_state.get("last_patch_branch")
        and not st.session_state.get("merge_result")
    )
    scan_column, review_column = st.columns(2)
    with scan_column:
        scan_clicked = st.button(
            "Scan project",
            type="primary",
            use_container_width=True,
            disabled=workflow_locked,
            help=(
                "Finish the active patch's PR and checks before starting another scan."
                if workflow_locked
                else "Run Semgrep on eligible files in the selected folder and list its findings."
            ),
        )
    with review_column:
        full_review_clicked = st.button(
            "Get full AI review",
            use_container_width=True,
            disabled=workflow_locked,
            help=(
                "Finish the active patch's PR and checks before starting another review."
                if workflow_locked
                else "Send eligible source files in bounded chunks to Gemini for a broader review."
            ),
        )

    if workflow_locked:
        st.info(
            f"A patch is already applied on branch `{st.session_state['last_patch_branch']}`. "
            "Continue with its draft PR and checks below; SentinEX will enable another scan "
            "after the merge is complete."
        )

    if scan_clicked and not workflow_locked:
        for key in (
            "findings",
            "scan_report",
            "scan_error",
            "analysis",
            "full_review",
            "gemini_prompt",
            "last_patch_branch",
            "last_patch_root",
            "last_patch_file",
            "post_patch_result",
            "post_patch_findings",
            "post_patch_scan_report",
            "created_pr_result",
            "merge_result",
            "full_review_prompts",
        ):
            st.session_state.pop(key, None)
        try:
            root = resolve_project_root(project_path)
            with st.spinner("Scanning the selected project with Semgrep..."):
                report = scan_project(root)
            findings = report.findings
            st.session_state["project_root"] = str(root)
            st.session_state["findings"] = findings
            st.session_state["scan_report"] = {
                "files": len(report.scanned_files),
                "engine": report.engine,
                "skipped": report.skipped_files,
            }
            for key in list(st.session_state):
                if key.startswith("patch_proposal_"):
                    del st.session_state[key]
            if findings:
                st.session_state["gemini_prompt"] = findings_prompt(root, findings)
        except (OSError, ValueError, ScanError, AIReviewError) as error:
            st.session_state["scan_error"] = str(error)
        except Exception as error:
            st.session_state["scan_error"] = f"Unexpected scan failure: {error}"

    scan_report = st.session_state.get("scan_report")
    if scan_report:
        finding_count = len(st.session_state.get("findings", []))
        st.success(
            f"Semgrep scanned {scan_report['files']} eligible file(s) with "
            f"{scan_report['engine']} and found {finding_count} finding(s)."
        )
        if scan_report.get("skipped"):
            st.warning(
                f"Semgrep skipped {len(scan_report['skipped'])} file(s). "
                "Review scan coverage before relying on these results."
            )
        if finding_count == 0:
            st.info(
                f"No matches were found in the {scan_report['files']} files Semgrep scanned. "
                "This does not prove the project is vulnerability-free."
            )
    elif st.session_state.get("scan_error"):
        st.error(f"Scan failed: {st.session_state['scan_error']}")

    if full_review_clicked and not workflow_locked:
        try:
            root = resolve_project_root(project_path)
            prompts = full_review_prompts(root)
            st.session_state["full_review_prompts"] = prompts
            st.session_state.pop("full_review", None)
            st.session_state["project_root"] = str(root)
            if not api_key_configured():
                st.error("Add your Gemini API key to .env before requesting a full review.")
            elif not prompts:
                st.info("No eligible source files were found for a full AI review.")
            else:
                with st.spinner(f"Gemini is reviewing {len(prompts)} bounded code chunk(s)..."):
                    st.session_state["full_review"] = review_prompts(prompts)
        except (OSError, ValueError, AIReviewError) as error:
            st.error(f"Full AI Review failed: {error}")
        except Exception as error:
            st.error(f"Full AI Review failed: {error}")

    workflow_locked = bool(
        st.session_state.get("last_patch_branch")
        and not st.session_state.get("merge_result")
    )
    findings = st.session_state.get("findings", [])
    if findings and not workflow_locked:
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
            f"SentinEX prepared {len(full_prompts)} bounded prompt chunk(s), with eligible source "
            "text redacted and capped. Clicking Get full AI review sends these chunks to Gemini. "
            "If Gemini fails partway through, later chunks may not be sent."
        )
        with st.expander("Preview all full-review chunks"):
            st.code("\n\n".join(full_prompts), language="text")
    if st.session_state.get("full_review"):
        st.subheader("Full AI Review")
        st.markdown(st.session_state["full_review"])

    st.divider()
    st.caption(
        "An approved patch changes the selected repository locally on a new Git branch. "
        "SentinEX creates a draft PR before local checks and only requests a merge after your explicit approval."
    )


if __name__ == "__main__":
    main()

