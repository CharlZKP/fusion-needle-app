"""Static checks of the Windows launcher and build script. They cannot be run on this OS;
these tests pin the properties that were reviewed (packaging/README.md)."""
import re

from .kit import DESKTOP

BAT = DESKTOP / "Start Fusion Needle.bat"
PS1 = DESKTOP / "packaging" / "build_windows.ps1"


def test_batch_launcher_is_crlf_ascii_and_keeps_arguments_out_of_blocks():
    raw = BAT.read_bytes()
    raw.decode("ascii")                                     # cmd reads it in the OEM code page
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert raw.count(b"\n") == raw.count(b"\r\n") > 20      # every line ends in CRLF
    lines = raw.decode("ascii").split("\r\n")
    assert lines[0] == "@echo off"
    depth = 0
    for number, line in enumerate(lines, 1):
        code = line.strip()
        if code.lower().startswith("rem"):
            assert "%" not in code, f"line {number}: cmd expands %...% even in a comment"
            continue
        bare = re.sub(r'"[^"]*"', '""', code).replace("^(", "").replace("^)", "")
        if depth > 0 or bare.endswith("("):
            assert "%*" not in code, f"line {number}: %* inside a ( ) block breaks on an argument with ')'"
        depth += bare.count("(") - bare.count(")")
        assert depth >= 0, f"line {number}: unbalanced brackets"
        for path in re.findall(r"%~dp0[^\s\"]*", code):     # every use of the script folder is inside quotes
            assert f'{path}"' in code or f'"{path}' in code, f"line {number}: unquoted {path}"
    assert depth == 0
    text = "\r\n".join(lines)
    assert "DisableDelayedExpansion" in text and "EnableDelayedExpansion" not in text
    assert text.count("sys.maxsize > 2**32") == 2           # py launcher and PATH python: 64-bit only
    assert '"%PYEXE%" %PYARGS% "%~dp0fusion_needle.py" %*' in text
    labels = {line[1:].strip() for line in lines if line.startswith(":")}
    assert set(re.findall(r"goto (\w+)", text)) <= labels


def test_build_script_avoids_the_powershell_5_pitfalls():
    raw = PS1.read_bytes()
    raw.decode("ascii")                                     # 5.1 reads a BOM-less script as ANSI
    text = raw.decode("ascii")
    code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    assert code[0].startswith("param(")                     # param must be the first statement
    assert '$ErrorActionPreference = "Stop"' in text
    for line in code:
        # stderr of a native command must not be redirected under ErrorActionPreference Stop
        assert not re.search(r"^\s*&.*\s2>", line), line
        assert not re.search(r"`\s+$", line), "a backtick continuation must end the line: " + line
        if re.match(r"\s*(Test-Path|Copy-Item|Remove-Item)\b", line) or "(Test-Path " in line:
            assert "-LiteralPath" in line or "Env:" in line, line
    native = [index for index, line in enumerate(code) if re.match(r"\s*(\$\w+ = )?& \$", line)]
    assert len(native) >= 6
    for index in native:                                    # every native call is followed by an exit-code check
        following = " ".join(code[index:index + 4])
        assert "$LASTEXITCODE" in following, code[index]
    assert "find_spec('PyInstaller')" in text               # asked on every run, not only when the venv is new
    assert "packaging\\README.md" in text


def test_packaging_readme_says_what_is_untested_and_what_to_send_back():
    text = (DESKTOP / "packaging" / "README.md").read_text(encoding="utf-8")
    assert "Nothing in this file has been run on Windows" in text
    for needed in ("build_windows.ps1", "-ExecutionPolicy Bypass", "Start Fusion Needle.bat", "End task",
                   "Check Fusion", "Export diagnostics", "what to send back", "model-server.log", "-Console"):
        assert needed in text, needed
    attributes = (DESKTOP / ".gitattributes").read_text(encoding="utf-8")
    assert "*.bat      text eol=crlf" in attributes
