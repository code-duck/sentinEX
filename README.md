# SentinEX

SentinEX is a local application-security review prototype. It scans eligible source code in one selected folder with Semgrep, sends findings and nearby code context to Gemini when configured, and offers a separate full AI review action.

## Current state

- Local Streamlit UI and selected-folder scan flow.
- Eligible source languages: Python, JavaScript, TypeScript, Java, Go, PHP, Ruby.
- Semgrep findings can be previewed with redacted nearby context before an explicit Gemini analysis request.
- Full AI Review previews eligible files in bounded chunks before an explicit Gemini request.
- Gemini can draft a minimal unified diff for a finding. The developer must inspect and approve it before SentinEX applies it to a new local Git branch.
- Patch application requires a clean Git working tree and refuses paths outside the selected project.
- Developer-approved test commands can run in Docker against a disposable copy of the selected project. The host project is mounted read-only; the container has no network, dropped Linux capabilities, and CPU, memory, process, and time limits.
- After the container run, SentinEX rescans the project with Semgrep and shows the remaining findings.
- GitHub PRs are opened as drafts through GitHub's official MCP server after explicit developer approval; SentinEX does not merge them.
- Semgrep does not guarantee complete vulnerability detection.

## Code layout

- `app.py` contains the Streamlit interface.
- `sentinex/project.py` enforces source-file selection and redacts common secrets.
- `sentinex/scanner.py` runs Semgrep locally and falls back to its official Docker image if Windows blocks the local executable.
- `sentinex/ai_review.py` handles Gemini explanations, patch proposals, and full review.
- `sentinex/patching.py` validates a proposed diff and applies it only after approval.
- `sentinex/docker_runner.py` runs one developer-entered test command with container isolation limits.
- `sentinex/github_pr.py` publishes the approved fix branch and creates a draft PR through the official GitHub MCP server.

## Setup

Python 3.14 is installed on this machine. Open this folder in VS Code, then use its terminal:

    cd E:\SentinEX
    py -3.14 -m venv .venv
    .\.venv\Scripts\Activate.ps1
    python -m pip install -r requirements.lock.txt

The current dependency environment is already created at E:\SentinEX\.venv.

## Gemini key

Edit the local `.env` file and add your Google AI Studio API key and GitHub fine-grained token when you are ready to use those integrations. The project has a blank `.env` prepared for you:

    GEMINI_API_KEY=your-private-key
    GEMINI_MODEL=gemini-3.8-flash
    GITHUB_PERSONAL_ACCESS_TOKEN=your-repository-scoped-token

Never commit `.env` or paste credentials into chat. Gemini API billing is separate from a Gemini Plus subscription. The GitHub token should be fine-grained and limited to the target repository, with permission to create pull requests. Git's configured credential manager is used to push the branch. The project you scan must have a GitHub `origin` remote and a fetched default branch.

## Run

    .\.venv\Scripts\python.exe -m streamlit run app.py

Enter a project folder path in the sidebar and choose Scan project. Expand the Gemini review preview to inspect the exact findings and redacted code context; choose Analyze with Gemini to send it. Expand a finding to ask Gemini for a patch, inspect the diff, and approve it before local application. The separate Full AI Review action previews and sends eligible source files in chunks to Gemini.

## Safety notes

- The scanner reads eligible source files only under the selected root and skips common generated/dependency folders.
- If Windows blocks the local Semgrep executable, the scanner can fall back to the official Semgrep Docker image. That fallback has network access to retrieve the configured rule pack; the selected project stays mounted read-only and source is not sent to Semgrep as an upload.
- Code is sent to Gemini only when an API key is configured and the user starts the relevant action.
- Patch changes are applied only after explicit approval, to a clean Git repository, on a new branch.
- Docker test images must contain the test tools and dependencies already; the container has no network and changes only its disposable copy. Docker Desktop may download a missing image before the container starts.
- A failing test run is still followed by a Semgrep rescan so the developer can see both outcomes.
- PR creation requires an `origin` GitHub remote, a published branch, and the GitHub MCP token; SentinEX commits and pushes only the approved single-file patch after a separate confirmation.
- Draft PRs are opened for human review. SentinEX never merges or deploys.

