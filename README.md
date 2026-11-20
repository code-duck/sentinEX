# SentinEX

SentinEX is a local application-security review prototype. It scans eligible source code in one selected folder with Semgrep, sends findings and nearby code context to Gemini when configured, and offers a separate full AI review action.

## Current state

- Local Streamlit UI and selected-folder scan flow.
- Eligible source languages: Python, JavaScript, TypeScript, Java, Go, PHP, Ruby.
- Semgrep findings can be analyzed with the Gemini API.
- Full AI Review sends eligible files to Gemini in bounded chunks.
- Gemini can draft a minimal unified diff for a finding. The developer must inspect and approve it before SentinEX applies it to a new local Git branch.
- Patch application requires a clean Git working tree and refuses paths outside the selected project.
- Developer-approved test commands can run in Docker with the project mounted read-only, no container network, dropped Linux capabilities, and CPU, memory, process, and time limits.
- After the container run, SentinEX rescans the project with Semgrep and shows the remaining findings.
- GitHub PR creation is the next milestone; developers will review and merge PRs themselves.
- Semgrep does not guarantee complete vulnerability detection.

## Code layout

- `app.py` contains the Streamlit interface.
- `sentinex/project.py` enforces source-file selection and redacts common secrets.
- `sentinex/scanner.py` runs Semgrep locally.
- `sentinex/ai_review.py` handles Gemini explanations, patch proposals, and full review.
- `sentinex/patching.py` validates a proposed diff and applies it only after approval.
- `sentinex/docker_runner.py` runs one developer-entered test command with container isolation limits.

## Setup

Python 3.14 is installed on this machine. Open this folder in VS Code, then use its terminal:

    cd E:\SentinEX
    py -3.14 -m venv .venv
    .\.venv\Scripts\Activate.ps1
    python -m pip install -r requirements.lock.txt

The current dependency environment is already created at E:\SentinEX\.venv.

## Gemini key

Copy .env.example to .env and put your Google AI Studio API key in .env:

    GEMINI_API_KEY=your-private-key
    GEMINI_MODEL=gemini-3.8-flash

Never commit .env. The API key is separate from a Gemini Plus subscription.

## Run

    .\.venv\Scripts\python.exe -m streamlit run app.py

Enter a project folder path in the sidebar and choose Scan and analyze. Expand a finding to ask Gemini for a patch, inspect the diff, and approve it before local application. The separate Full AI Review action sends eligible source files in chunks to Gemini.

## Safety notes

- The scanner reads eligible source files only under the selected root and skips common generated/dependency folders.
- Code is sent to Gemini only when an API key is configured and the user starts the relevant action.
- Patch changes are applied only after explicit approval, to a clean Git repository, on a new branch.
- Docker test images must contain the test tools and dependencies already; container networking is disabled and the project mount is read-only. Docker Desktop may download a missing image before the container starts.
- A failing test run is still followed by a Semgrep rescan so the developer can see both outcomes.
- GitHub PR creation is not implemented yet. SentinEX does not merge or deploy.

