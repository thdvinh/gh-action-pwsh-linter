#!/usr/bin/env python3
"""
GitHub Actions PowerShell Script Linter

Analyzes PowerShell script steps in GitHub Actions YAML workflow files
using the native PowerShell PSParser syntax validator (no execution).
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

import yaml

_GHA_EXPR_RE = re.compile(r"\$\{\{.+?\}\}", re.DOTALL)

# Force UTF-8 environment variables on Windows
if sys.platform == "win32":
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class ShellKind(str, Enum):
    PWSH = "pwsh"
    POWERSHELL = "powershell"
    SH = "sh"
    CMD = "cmd"


@dataclass
class PSValidationError:
    message: str
    line: int
    column: int
    offset: int = field(default=0, init=False)


@dataclass
class PSScriptStep:
    workflow_file: str
    job_name: str
    step_name: str
    step_index: int
    shell: ShellKind
    script: str
    errors: list[PSValidationError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    gha_expressions: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0

    @property
    def error_count(self) -> int:
        return len(self.errors)


# ---------------------------------------------------------------------------
# YAML extraction
# ---------------------------------------------------------------------------

def _shell_to_kind(raw: str) -> ShellKind:
    raw_lower = raw.strip().lower()
    if raw_lower in ("pwsh", "pwsh -e"):
        return ShellKind.PWSH
    if raw_lower.startswith("powershell"):
        return ShellKind.POWERSHELL
    return ShellKind.SH  # default


def _resolve_default_shell(doc: dict, job_def: dict) -> ShellKind:
    """Resolve effective default shell: workflow defaults -> job defaults."""
    # Workflow-level defaults
    wf_defaults = doc.get("defaults") or {}
    wf_run = wf_defaults.get("run") or ""
    if isinstance(wf_run, str) and wf_run.strip():
        return _shell_to_kind(wf_run)
    if isinstance(wf_run, dict):
        wf_shell = wf_run.get("shell", "")
        if isinstance(wf_shell, str) and wf_shell.strip():
            return _shell_to_kind(wf_shell)

    # Job-level defaults
    job_defaults = job_def.get("defaults") or {}
    job_run = job_defaults.get("run") or ""
    if isinstance(job_run, str) and job_run.strip():
        return _shell_to_kind(job_run)
    if isinstance(job_run, dict):
        job_shell = job_run.get("shell", "")
        if isinstance(job_shell, str) and job_shell.strip():
            return _shell_to_kind(job_shell)

    return ShellKind.SH


def extract_ps_steps(yaml_path: Path) -> list[PSScriptStep]:
    """Walk the parsed YAML tree and collect all PowerShell `run` steps."""
    with open(yaml_path, "r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)

    if not isinstance(doc, dict):
        return []

    steps: list[PSScriptStep] = []
    jobs = doc.get("jobs") or {}

    for job_name, job_def in jobs.items():
        if not isinstance(job_def, dict):
            continue

        job_steps = job_def.get("steps") or []
        default_shell = _resolve_default_shell(doc, job_def)

        for idx, step in enumerate(job_steps):
            if not isinstance(step, dict):
                continue

            # Resolve effective shell: step-level overrides job/wf defaults
            step_shell_raw = step.get("shell", "")
            if step_shell_raw and isinstance(step_shell_raw, str) and step_shell_raw.strip():
                kind = _shell_to_kind(step_shell_raw)
            else:
                kind = default_shell

            # Only analyse pwsh / powershell shell steps
            if kind not in (ShellKind.PWSH, ShellKind.POWERSHELL):
                continue

            run_val = step.get("run")
            if not isinstance(run_val, str) or not run_val.strip():
                continue

            step_name = step.get("name") or f"step #{idx + 1}"
            script = run_val.strip()

            steps.append(PSScriptStep(
                workflow_file=str(yaml_path),
                job_name=str(job_name),
                step_name=str(step_name),
                step_index=idx + 1,
                shell=kind,
                script=script,
            ))

    return steps


# ---------------------------------------------------------------------------
# PowerShell PSParser validation
# ---------------------------------------------------------------------------

VALIDATE_PS_SCRIPT = r"""
# Force UTF-8 for consistent output
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::InputEncoding  = [System.Text.Encoding]::UTF8

$errors  = $null
$tokens = $null

# Use Language.Parser (modern) over legacy PSParser for richer error info
$tokens = [System.Management.Automation.Language.Parser]::ParseInput($Script, [ref]$tokens, [ref]$errors)

$result = @{ errors = @(); warnings = @(); valid = $true }

foreach ($err in $errors) {
    # Some errors are benign re-parse warnings from the parser itself
    # (the parser calls ParseInput recursively). Skip those that have no extent.
    if (-not $err.Extent) {
        continue
    }

    $msg  = $err.Message
    $line = $err.Extent.StartLineNumber
    $col  = $err.Extent.StartColumnNumber

    # Downgrade certain warnings (e.g. 'Unexpected token in' for pipeline
    # operators in certain positions) to warnings rather than hard errors.
    if ($msg -match "Unexpected token 'in'") {
        $result.warnings += @{ message = $msg; line = $line; column = $col }
        continue
    }

    $result.valid     = $false
    $result.errors += @{ message = $msg; line = $line; column = $col }
}

# --- brace / bracket / paren balance (catches missing closers) -----------
$balance = @{
    'LeftCurlyBracket'     = 0; 'RightCurlyBracket'     = 0
    'LeftSquareBracket'    = 0; 'RightSquareBracket'    = 0
    'LeftParen'            = 0; 'RightParen'            = 0
}

foreach ($t in $tokens) {
    switch ($t.GetType().Name) {
        # Language.Parser token type names
        'Token' {
            $type = $t.Type.ToString()
            switch -exact ($type) {
                'LeftCurlyBracket'      { $balance['LeftCurlyBracket']++   }
                'RightCurlyBracket'     { $balance['RightCurlyBracket']++  }
                'LeftSquareBracket'     { $balance['LeftSquareBracket']++  }
                'RightSquareBracket'    { $balance['RightSquareBracket']++ }
                'LeftParen'             { $balance['LeftParen']++          }
                'RightParen'            { $balance['RightParen']++         }
            }
        }
    }
}

$lastToken = $tokens[-1]
$endLine   = if ($lastToken) { $lastToken.EndLineNumber } else { 1 }

foreach ($pair in @([PSCustomObject]@{
        Open  = 'LeftCurlyBracket'
        Close = 'RightCurlyBracket'
        Chars = @('}', '}')
        Label = 'brace(s) ''}}'''
    },
    [PSCustomObject]@{
        Open  = 'LeftSquareBracket'
        Close = 'RightSquareBracket'
        Chars = @(']', ']')
        Label = 'bracket(s)'
    },
    [PSCustomObject]@{
        Open  = 'LeftParen'
        Close = 'RightParen'
        Chars = @(')', ')')
        Label = 'parenthesis(s) '')'''
    })) {
    $diff = $balance[$pair.Open] - $balance[$pair.Close]
    if ($diff -gt 0) {
        $result.valid       = $false
        $result.errors += @{
            message  = "Missing $diff closing $($pair.Label)"
            line     = $endLine
            column   = 1
        }
    }
}

# Serialize to JSON for the Python side
$json = ConvertTo-Json $result -Compress
Write-Output $json
"""


def _validate_script_powershell(script: str) -> tuple[list[dict], list[dict], bool, list[str]]:
    """
    Invoke pwsh with the PSParser inline script using a temp file.
    Strips GitHub Actions interpolation (e.g. ${{ env.X }}) before parsing
    so those don't produce false syntax errors.
    Returns (errors, warnings, is_valid, list_of_gha_expressions).
    """
    # Extract all GHA interpolation expressions
    gha_exprs = list(set(_GHA_EXPR_RE.findall(script)))

    # Replace with a placeholder so the PowerShell parser sees valid syntax
    sanitized = _GHA_EXPR_RE.sub('__GHAEXPR__', script)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".ps1", delete=False, encoding="utf-8",
    ) as fh:
        # 1. The script content becomes the `$Script` variable
        # Use single-quoted here-string @'...'@ to prevent variable expansion
        fh.write("$Script = @'\n")
        fh.write(sanitized)
        if not sanitized.endswith("\n"):
            fh.write("\n")
        fh.write("'@;\n")
        # 2. Append the validation body
        fh.write(VALIDATE_PS_SCRIPT)
        validate_path = fh.name

    try:
        proc = subprocess.run(
            ["pwsh", "-NoProfile", "-NonInteractive", "-File", validate_path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )

        stdout = proc.stdout.strip()
        if not stdout:
            err = proc.stderr.strip()
            raised = proc.returncode != 0
            if raised:
                raise RuntimeError(f"pwsh failed: {err}")
            return [], [], True, gha_exprs

        try:
            result = json.loads(stdout)
        except json.JSONDecodeError:
            raise RuntimeError(f"Unexpected pwsh output:\n{stdout}\n---\n{proc.stderr}")

        errors = result.get("errors", [])
        warnings = result.get("warnings", [])
        valid = result.get("valid", False)

        return errors, warnings, valid, gha_exprs
    finally:
        try:
            Path(validate_path).unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Report helpers
# ---------------------------------------------------------------------------

_STEP_CHAR = "\\---"


def _fmt_step_header(step: PSScriptStep) -> str:
    wf = Path(step.workflow_file)
    try:
        rel = str(wf.relative_to(Path.cwd()))
    except ValueError:
        rel = str(wf)
    return (
        f"{_STEP_CHAR} [{step.step_index}] {step.step_name}  "
        f"({rel}: job:{step.job_name})"
    )


def _print_summary(results: list[PSScriptStep], show_valid: bool = True) -> int:
    total = len(results)
    valid = sum(1 for r in results if r.is_valid)
    failed = total - valid

    if show_valid:
        print()
        print("=" * 72)
        print("  GitHub Actions PowerShell Linter — Summary")
        print("=" * 72)
        print(f"  Total PS steps analysed : {total}")
        print(f"  Valid                   : {valid}  ")
        print(f"  Errors                  : {failed}  ")
        print("=" * 72)
    return failed


def _print_results_html(results: list[PSScriptStep], output_path: Path) -> None:
    """Write an HTML report for nicer browser viewing."""
    rows = []
    for s in results:
        for err in s.errors:
            rows.append({
                "file":      s.workflow_file,
                "job":       s.job_name,
                "step":      s.step_name,
                "step_id":   s.step_index,
                "line":      err.line,
                "column":    err.column,
                "message":   err.message,
                "shell":     s.shell.value,
            })

    header = f"""<html>
<head>
  <meta charset="UTF-8">
  <title>PS Linter Report</title>
  <style>
    body {{ font-family: 'Segoe UI', monospace; margin: 2em; background: #1e1e2e; color: #cdd6f4; }}
    h1 {{ color: #f38ba8; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 1em; }}
    th, td {{ text-align: left; padding: 8px 12px; border-bottom: 1px solid #45475a; }}
    th {{ background: #313244; color: #89b4fa; }}
    tr:hover {{ background: #313244; }}
    .badge {{ padding: 2px 8px; border-radius: 4px; font-size: 0.85em; }}
    .pwsh {{ background: #45475a; color: #a6e3a1; }}
    .ps {{ background: #45475a; color: #f9e2af; }}
    .code {{ background: #181825; padding: 2px 6px; border-radius: 3px; font-family: monospace; }}
    pre.error-code {{ white-space: pre-wrap; background: #181825; padding: 1em; border-radius: 6px; color: #fab387; }}
  </style>
</head>
<body>
<h1>GitHub Actions &mdash; PowerShell Linter Report</h1>
<p>Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
<table>
<tr><th>File</th><th>Job</th><th>Step</th><th>Shell</th><th>Line</th><th>Column</th><th>Message</th></tr>
"""
    for r in rows:
        rows_html = f"""<tr>
    <td><span class="code">{r["file"]}</span></td>
    <td>{r["job"]}</td>
    <td>{r["step"]}</td>
    <td><span class="badge {r["shell"]}">{r["shell"]}</span></td>
    <td>{r["line"]}</td>
    <td>{r["column"]}</td>
    <td>{r["message"]}</td>
  </tr>
"""
        header += rows_html

    footer = """</table>
</body>
</html>\n"""
    output_path.write_text(header + footer, encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _process_steps(steps: list[PSScriptStep]) -> list[PSScriptStep]:
    """Run PSParser validation on every extracted step."""
    for step in steps:
        try:
            errs, warns, valid, exps = _validate_script_powershell(step.script)
        except Exception as exc:
            step.errors.append(PSValidationError(
                message=str(exc), line=0, column=0
            ))
            continue

        for e in errs:
            step.errors.append(PSValidationError(
                message=str(e["message"]),
                line=int(e["line"]),
                column=int(e["column"]),
            ))
        for w in warns:
            step.warnings.append(str(w["message"]))
        step.gha_expressions = exps

    return steps


def _rich_print(results: list[PSScriptStep]) -> None:
    """Print a human-readable report to stdout."""
    has_any_errors = any(r.errors for r in results)

    for r in results:
        if r.error_count > 0 or (not has_any_errors and r.warnings):
            print()
            print(_fmt_step_header(r))
            print()

            for err in r.errors:
                # Find the offending line in the original script
                offending = ""
                if err.line > 0:
                    lines = r.script.splitlines()
                    if err.line <= len(lines):
                        offending = lines[err.line - 1]

                print(f"  [ERR] Line {err.line}, Col {err.column}: {err.message}")
                if offending:
                    print(f"         {offending}")

            for w in r.warnings:
                print(f"  [WRN] {w}")

            if r.gha_expressions:
                print(f"  [INFO] GitHub Actions expressions found - verify these exist:")
                for expr in r.gha_expressions:
                    print(f"           {expr}")
            continue

        if r.is_valid and r.gha_expressions:
            print()
            print(f"[OK] {_fmt_step_header(r)}")
            print(f"  [INFO] GitHub Actions expressions found - verify these exist:")
            for expr in r.gha_expressions:
                print(f"           {expr}")
            continue

        if r.is_valid:
            print()
            print(f"[OK] {_fmt_step_header(r)}")


def _dict_results(results: list[PSScriptStep]) -> list[dict]:
    out = []
    for r in results:
        out.append({
            "file":             r.workflow_file,
            "job":              r.job_name,
            "step":             r.step_name,
            "step_id":          r.step_index,
            "shell":            r.shell.value,
            "valid":            r.is_valid,
            "errors":           [asdict(e) for e in r.errors],
            "warnings":         r.warnings,
            "gha_expressions":  r.gha_expressions,
        })
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Lint PowerShell scripts inside GitHub Actions YAML workflows.",
    )
    ap.add_argument(
        "workflow", nargs="+", type=Path,
        help="Path(s) to the GitHub Actions YAML file(s).",
    )
    ap.add_argument(
        "--format", "-f",
        choices=["text", "json", "html"],
        default="text",
        help="Output format (default: text).",
    )
    ap.add_argument(
        "--output", "-o", type=Path, default=None,
        help="Output file (required for html format).",
    )
    ap.add_argument(
        "--fail-on",
        choices=["errors", "warnings"],
        default="errors",
        help="Exit non-zero when these are present (default: errors).",
    )
    ap.add_argument(
        "--no-summary", action="store_true",
        help="Suppress the summary table.",
    )
    args = ap.parse_args(argv)

    all_steps: list[PSScriptStep] = []
    for wf in args.workflow:
        if not wf.exists():
            print(f"[ERROR] File not found: {wf}", file=sys.stderr)
            return 1
        all_steps.extend(extract_ps_steps(wf))

    if not all_steps:
        print("No PowerShell script steps found in the provided workflow(s).")
        return 0

    all_steps = _process_steps(all_steps)

    # ---- output -----------------------------------------------------------
    if args.format == "json":
        print(json.dumps(_dict_results(all_steps), indent=2))
        return 0

    if args.format == "html":
        out = args.output
        if not out:
            out = Path.cwd() / "ps-linter-report.html"
        _print_results_html(all_steps, out)
        print(f"HTML report written to: {out}")
        return 0

    # text (default)
    _rich_print(all_steps)
    failed = _print_summary(all_steps, show_valid=not args.no_summary)

    # ---- exit code --------------------------------------------------------
    if args.fail_on == "errors" and failed > 0:
        return 1
    if args.fail_on == "warnings":
        warn_count = sum(len(r.warnings) for r in all_steps)
        if warn_count > 0:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())