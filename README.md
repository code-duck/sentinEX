# SentinEX

SentinEX is a local-first application security review prototype. It combines Semgrep Community Edition findings with Gemini explanations and proposed patches, then guides a developer through local approval, a GitHub draft pull request, isolated tests, a rescan, and an explicitly approved merge.

Its intended first users are AI-assisted solo developers and small software teams that use GitHub but do not have a dedicated application security specialist. SentinEX is a developer aid; it does not replace a security review or guarantee that code is vulnerability-free.

## What it does

- Opens a selected project folder with the Windows folder picker or accepts a path.
- Runs Semgrep using `p/security-audit` plus SentinEX's included local rules for common unsafe code patterns. The scanner recognizes rule-matched code patterns; unique code may still contain issues the rules do not cover.
- Shows findings and a preview of the redacted finding context before **Analyze with Gemini** sends it.
- Offers **Ask Gemini for a patch** for an individual finding. If the configured model returns a 503, SentinEX tries the configured fallback model list. Gemini proposes a diff; it does not edit files by itself.
- Provides clearly labeled, local rule-based fallback fixes for recognized `subprocess-shell-true`, Python `eval`, and unverified TLS findings. They are not AI-generated and are shown as diffs for developer approval before any file changes.
- Shows the diff and changes the selected project only after the developer approves it. Patch application is restricted to a clean Git repository, a new SentinEX branch, and one file.
- After patch approval, offers a separate confirmation to push the branch and create a GitHub draft PR.
- After draft PR creation, detects common test setups and suggests a relevant command. The developer reviews and approves the exact Docker image and command; SentinEX then runs it in Docker and rescans with Semgrep. A merge action is available only after tests pass and the developer explicitly approves it. Remaining findings must be acknowledged.
- Provides **Get full AI review** as a separate action. Clicking it sends eligible source code to Gemini in bounded chunks; the app displays the prepared chunks after that request. Review code and secrets policy before using this action.
- Includes Windows desktop launch and stop scripts. The launcher hides the console, opens the local dashboard, and saves server output in `logs/` (the five most recent launch sessions are retained).

## Supported code and scan boundaries

SentinEX currently selects these source extensions inside the chosen folder:

| Language | Extensions |
| --- | --- |
| Python | `.py` |
| JavaScript | `.js`, `.jsx` |
| TypeScript | `.ts`, `.tsx` |
| Java | `.java` |
| Go | `.go` |
| PHP | `.php` |
| Ruby | `.rb` |

The selector skips common generated or dependency folders such as `.git`, `.venv`, `node_modules`, `vendor`, `dist`, and `build`. It considers up to 500 files and skips files larger than 1.5 MB. Findings analysis includes a limited nearby-code excerpt. Common inline secret formats and Google API key patterns are redacted, but redaction is not comprehensive.

This prototype focuses on application source code. It does not currently provide full infrastructure-as-code, cloud configuration, CI/CD workflow, dependency/SCA, DAST, or binary scanning. Semgrep and AI review can miss vulnerabilities or report false positives.

## Workflow

1. Choose a local project folder and run **Scan project**.
2. Inspect Semgrep findings. Expand the Gemini preview before choosing **Analyze with Gemini** to request explanations.
3. For a finding, choose **Ask Gemini for a patch**. Review the proposed diff; then choose **Approve and apply this patch** if you accept it.
4. Review and confirm the branch push and draft PR creation in SentinEX. GitHub hosts the PR; open its link to review the actual GitHub diff.
5. Return to SentinEX and enter the project’s test command and a Docker image that already contains its test dependencies. SentinEX runs tests against a disposable copy, then rescans the patched project.
6. If the test command passes, inspect the PR, test output, and rescan. Explicitly approve the merge in SentinEX to request a squash merge. SentinEX does not deploy the project.

Scanning, patch application, and Docker test execution operate on the developer’s local machine. Gemini requests send the selected code context to Google’s API. GitHub MCP performs PR operations on GitHub. The app cannot merge unless the developer checks the approval box and submits the merge action; repository rules may still reject a merge.

## Technology

| Component | Role |
| --- | --- |
| Python | Project scoping, orchestration, patch validation, Git and API workflow |
| Streamlit | Local browser dashboard |
| Semgrep CE | Static analysis with `p/security-audit` plus SentinEX's local YAML rules |
| Gemini API (`google-genai`) | Finding explanations, patch proposals, optional whole-project review |
| Git for Windows | Local branches, commits, and pushing approved changes |
| Docker Desktop | Semgrep fallback, GitHub MCP container, and restricted test runner |
| GitHub MCP Server | Create draft PRs, mark them ready, and merge after explicit approval |

Gemini is the only AI provider currently implemented. A self-hosted model option is a possible future feature, not part of this version.

## Requirements

- Windows 10/11, Python 3.14, and Git for Windows.
- Docker Desktop with the Linux engine running. On Windows, SentinEX runs Semgrep in Docker, as well as restricted test runs and the GitHub MCP server.
- A Gemini API key for explanations, patches, and full AI review. A Gemini consumer subscription does not itself include API usage; API terms and billing/quota depend on the Google AI Studio project.
- For PR operations, a GitHub fine-grained token limited to the target repository, with repository metadata read access, contents read/write, and pull requests read/write. Git’s configured credential manager is used to push the branch; the token is passed to the GitHub MCP container for PR operations.

## Setup

From PowerShell in the project folder:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock.txt
Copy-Item .env.example .env
```

Edit `.env` locally:

```dotenv
GEMINI_API_KEY=your_google_ai_studio_key
GEMINI_MODEL=gemini-3.8-flash
GEMINI_FALLBACK_MODELS=gemini-3.5-flash-lite
GITHUB_PERSONAL_ACCESS_TOKEN=your_repository_scoped_token
```

Never commit `.env` or paste credentials into public issues or chat. `.env` is ignored by Git. The model name can be changed to a model currently available to your API project. Gemini may return `503 UNAVAILABLE` during service overload; this is an upstream availability issue, not fixed by creating another key.

## Run SentinEX

### Desktop shortcuts (Windows)

The desktop **SentinEX** shortcut starts the local server in the background and opens the browser. **Stop SentinEX** stops the server. The launcher scripts and icon assets are in this repository. Logs are stored in `logs/` and are excluded from Git.

### Terminal

```powershell
cd E:\SentinEX
.\.venv\Scripts\python.exe -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501
```

Open `http://127.0.0.1:8501`. Keep the terminal open while the app is running; press **Ctrl+C** to stop it.

## GitHub repository requirements

The project selected for remediation must be the root of a Git repository with a GitHub `origin` remote and a fetched default branch. The current PR path accepts only the approved one-file SentinEX patch commit. Branch pushing uses the developer’s Git credential manager. GitHub MCP runs from Docker using `ghcr.io/github/github-mcp-server:latest` and the repository-scoped token.

SentinEX’s own repository is `https://github.com/code-duck/sentinEX`. The intentionally vulnerable demo repository used for demonstrations is separate: `https://github.com/code-duck/SentinEX-Faulty-Demo`.

## Docker test limits and caveats

The test runner mounts the selected project read-only, copies it into disposable container storage, disables network access, drops Linux capabilities, sets CPU/memory/process limits, and enforces a timeout. SentinEX detects common Python, Node.js, Go, Maven, Gradle, Composer, and Ruby test setups where possible. Test dependencies must already be available in the selected image or project because the test container has no network access. The developer can change the suggested image and command before approving. These restrictions reduce risk but do not make arbitrary test commands or images perfectly safe. Use trusted project code, test commands, and container images. A missing image may be downloaded before the offline test container starts.

On Windows, Semgrep runs in the official Docker image with the selected project mounted read-only. It combines the configured Semgrep registry rules with SentinEX's local YAML rules. SentinEX verifies Semgrep's JSON diagnostics and scanned-file list; a failed or empty scan is shown as a failure rather than a clean result. Registry rules may be fetched over the network.

## Current prototype limitations

- The Gemini API can be temporarily unavailable or overloaded; AI explanation and patch generation then cannot complete.
- On 503 errors, SentinEX tries configured Gemini fallback models. If all are unavailable, the built-in fixes cover only a few narrowly recognized patterns; they are clearly labeled and are not AI responses.
- Full AI Review sends code when clicked and only then displays the prepared chunks; it currently has no separate pre-send confirmation screen.
- The full GitHub PR, Docker test, rescan, and merge sequence has not yet been demonstrated end to end with a successful Gemini patch. Review the draft PR directly on GitHub before approving any merge.
- No local/self-hosted model provider, multi-user/team administration, policy management, or enterprise audit console is implemented.
- Docker resource limits are safeguards, not a complete security boundary.

## Project structure

```text
app.py                    Streamlit dashboard and workflow
sentinex/project.py       Selected-folder boundaries, source selection, redaction
sentinex/scanner.py       Semgrep scan and Docker fallback
sentinex/security_rules.yml Local rules for selected common security mistakes
sentinex/ai_review.py     Gemini explanations, patch proposals, full review
sentinex/patching.py      Approval-gated local patch application
sentinex/docker_runner.py Restricted Docker test runner
sentinex/test_discovery.py Common test-setup detection and command suggestions
sentinex/github_pr.py     GitHub MCP draft PR and approved merge operations
sentinex/folder_picker.py Windows folder picker
launch_sentinex.ps1       Hidden Windows launcher with local log files
stop_sentinex.ps1         Verified SentinEX process stop action
assets/                   Desktop shortcut icons
```
