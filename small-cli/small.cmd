@echo off
rem small without pip install and without small.exe (Smart App Control may block unsigned .exe).
rem Add this folder to PATH and `small deploy` works from any folder.
rem -I: do not import modules from the current folder, so a user's app.py/json.py cannot shadow stdlib.
rem Keep this file ASCII-only with CRLF line endings: cmd.exe misparses UTF-8 text and LF-only files.
python -I -c "import sys; sys.path.insert(0, sys.argv.pop(1)); from small_cli.cli import main; sys.exit(main())" "%~dp0." %*
