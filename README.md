# GitHub Actions PowerShell Linter

A small command-line tool that finds PowerShell `run` steps in GitHub Actions workflow files and checks their syntax with PowerShell's parser. It reports parser errors without executing the scripts.

The linter also recognizes GitHub Actions expressions such as `${{ secrets.DEPLOY_KEY }}` and replaces them with placeholders during parsing to avoid false syntax errors. It reports the expressions it found for you to verify; it does not validate the expressions against a repository.

## Requirements

- Python 3.10 or later
- PowerShell 7 or later, with `pwsh` available on `PATH`

PowerShell is required even when the workflow declares `shell: powershell`: the linter invokes `pwsh` to parse each script. Python dependencies are listed in `requirements.txt`.

## Installation

Clone or download this repository, then from its directory create and activate a virtual environment and install the dependencies.

### Windows (PowerShell)

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

If PowerShell blocks activation, use the virtual environment's interpreter directly instead:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### macOS or Linux

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Check that both executables are available:

```sh
python --version
pwsh --version
```

## User Guide

Run the linter from the repository directory, giving it one or more workflow YAML files:

```sh
python ps_linter.py .github/workflows/build.yml
```

The default text report lists errors with the workflow, job, step, and line and column within that step's `run` script. A summary shows the number of valid and failed PowerShell steps. GitHub Actions expressions found in a script are shown as informational items and should be checked against the workflow's context.

Pass multiple files explicitly to check several workflows:

```sh
python ps_linter.py .github/workflows/build.yml .github/workflows/release.yml
```

### Output formats

Text is the default. JSON is written to standard output:

```sh
python ps_linter.py .github/workflows/build.yml --format json
```

HTML produces a report file. The default output name is `ps-linter-report.html`; set a different path with `--output`:

```sh
python ps_linter.py .github/workflows/build.yml --format html --output report.html
```

### Exit status and CI

In the default text format, the command exits with status `1` when syntax errors are found, and `0` otherwise. Use this in a CI step to fail a job on invalid PowerShell syntax:

```yaml
- name: Lint workflow PowerShell
  run: python ps_linter.py .github/workflows/build.yml
```

Use `--fail-on warnings` to return a non-zero status when parser warnings are present instead of errors. `--no-summary` suppresses the text summary. Note that JSON and HTML output modes currently return status `0` after generating output, regardless of lint findings; use the default text format when the process exit status should gate CI.

## What It Checks

- Only `run` steps whose effective shell is `pwsh` or `powershell` are analyzed. An explicit step-level shell takes precedence; otherwise, the linter checks the workflow-level default before the job-level default. If no PowerShell shell is specified, the workflow's default shell is treated as `sh` and its steps are skipped.
- The linter checks PowerShell syntax, not runtime behavior, commands, types, permissions, or whether a script will succeed when executed.
- GitHub Actions `${{ ... }}` expressions are hidden from the PowerShell parser during syntax checking. Their names and values are not verified.
- Workflows without PowerShell `run` steps produce a message and exit successfully.
- Input files are workflow YAML paths, not directories or repository-wide searches.

Use the official GitHub Actions documentation to learn about workflow syntax and expressions: <https://docs.github.com/actions>.