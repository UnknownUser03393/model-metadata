@echo off
rem Launcher so `modelmeta <command>` works with bin\ on PATH, with nothing installed.
rem
rem This file lives in bin\ rather than the repo root because a file named `modelmeta`
rem would collide with the `modelmeta\` package directory.
rem
rem Deliberately invokes `python`, never `python3`: on Windows `python3` commonly resolves
rem to a Microsoft Store alias stub that prints a "not found" message and exits 0, so a
rem launcher that preferred it would fail confusingly instead of loudly.
rem
rem .gitattributes pins *.cmd to CRLF; cmd.exe can mis-parse LF-only batch files.
setlocal
set "ROOT=%~dp0.."
set "PYTHONPATH=%ROOT%;%PYTHONPATH%"
python -m modelmeta %*
