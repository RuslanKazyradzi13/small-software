<#
.SYNOPSIS
    Installs the small CLI on Windows without the unsigned small.exe that Smart App Control blocks.

.DESCRIPTION
    "pip install small-software" creates small.exe, an unsigned launcher. Smart App Control and other
    Application Control policies block it ("Permission denied"). This script installs small another way:

      1. finds Python 3.9+ (the py launcher first, then python on PATH; the Microsoft Store alias is skipped);
      2. creates or upgrades a private venv in %LOCALAPPDATA%\Programs\small\venv and pip-installs
         small-software into it;
      3. writes %LOCALAPPDATA%\Programs\small\bin\small.cmd, which runs the venv's python.exe with
         "-I -m small_cli" (python.exe is signed, small.exe is not);
      4. puts that bin folder at the front of the user PATH.

    Running it again upgrades small in place. It needs no administrator rights and touches nothing outside
    the install folder and its user PATH entry.

    Environment variables for tests and CI:
      SMALL_INSTALL_DIR   install folder instead of %LOCALAPPDATA%\Programs\small
      SMALL_PACKAGE       what pip installs instead of small-software, for example a local wheel
      SMALL_NO_PATH=1     do not change PATH

    Keep this file ASCII-only: Windows PowerShell 5.1 reads UTF-8 files without a BOM as ANSI.

.PARAMETER Uninstall
    Remove the install folder and its user PATH entry.

.EXAMPLE
    irm https://raw.githubusercontent.com/RuslanKazyradzi13/small-software/main/install.ps1 | iex

.EXAMPLE
    & ([scriptblock]::Create((irm https://raw.githubusercontent.com/RuslanKazyradzi13/small-software/main/install.ps1))) -Uninstall

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall
#>
param([switch]$Uninstall)

# Everything runs in a script block. With "irm | iex" the code runs in the user's own session, and the block keeps
# StrictMode, $ErrorActionPreference and the helper functions out of it. For the same reason "exit" is used only
# when the script runs as a file: in "irm | iex" it would close the user's PowerShell window.
& {
    param([bool]$Uninstall, [string]$ScriptPath)

    $ScriptUrl = 'https://raw.githubusercontent.com/RuslanKazyradzi13/small-software/main/install.ps1'
    $IssuesUrl = 'https://github.com/RuslanKazyradzi13/small-software/issues'
    $RunAsFile = [bool]$ScriptPath

    if ($ExecutionContext.SessionState.LanguageMode -ne 'FullLanguage') {
        $mode = $ExecutionContext.SessionState.LanguageMode
        Write-Host "PowerShell runs in $mode mode here (set by an Application Control policy)," -ForegroundColor Red
        Write-Host 'and this installer needs FullLanguage. Install small with pip and run it through python:' -ForegroundColor Red
        Write-Host '    py -m pip install --upgrade small-software'
        Write-Host '    py -m small_cli --help'
        if ($RunAsFile) { exit 1 }
        return
    }

    Set-StrictMode -Version Latest
    $ErrorActionPreference = 'Stop'
    $PSNativeCommandUseErrorActionPreference = $false  # PowerShell 7.3+: exit codes are checked by hand below

    $MinPython = [version]'3.9'
    $MarkerName = 'small-installer.txt'
    $MarkerLines = @(
        'This folder was created by the small installer (install.ps1):'
        'https://github.com/RuslanKazyradzi13/small-software'
        ''
        'venv\  Python virtual environment with the small-software package'
        'bin\   small.cmd, the "small" command; this folder is on the user PATH'
        ''
        'To remove small, run the installer with -Uninstall.'
    )
    # The "small" command. The venv is found relative to the .cmd itself (%~dp0), so the file holds no absolute
    # path: it stays ASCII even when the user name is not (cmd.exe would read a non-ASCII path in the console code
    # page). %~dp0 is taken inside "call :here" because at the top level cmd.exe gets it wrong when the command is
    # typed with quotes ("small" init): it then resolves it against the current folder.
    $ShimLines = @(
        '@echo off'
        'rem small: created by install.ps1 from https://github.com/RuslanKazyradzi13/small-software'
        'rem Runs the small CLI with the python.exe of its own venv instead of the unsigned small.exe'
        'rem that pip creates, which Smart App Control blocks.'
        'rem -I (isolated mode): an app.py or json.py in the current folder cannot shadow the standard'
        'rem library. small_cli itself is in the site-packages of the venv.'
        'rem Keep this file ASCII-only with CRLF line endings: cmd.exe misparses anything else.'
        'setlocal'
        'call :here'
        '"%SMALL_SHIM_DIR%..\venv\Scripts\python.exe" -I -m small_cli %*'
        'exit /b %ERRORLEVEL%'
        ':here'
        'set "SMALL_SHIM_DIR=%~dp0"'
        'exit /b'
    )

    # --- output -------------------------------------------------------------------------------------------------

    function Write-Step([string]$Text) { Write-Host "==> $Text" -ForegroundColor Cyan }
    function Write-Info([string]$Text) { Write-Host "    $Text" }
    function Write-Warn([string]$Text) { Write-Host "Warning: $Text" -ForegroundColor Yellow }

    # An expected failure: the message is shown without a stack trace.
    function Stop-Install([string]$Message) {
        $exception = New-Object System.InvalidOperationException $Message
        $exception.Data['small'] = $true
        throw $exception
    }

    # Hides user:password@ in a URL-like package spec, e.g. git+https://user:token@host/repo.
    function Hide-Secret([string]$Text) { return ($Text -replace '(?<=://)[^/@\s]+@', '***@') }

    # --- files --------------------------------------------------------------------------------------------------

    function Get-InstallDir {
        if ($env:SMALL_INSTALL_DIR) {
            $dir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($env:SMALL_INSTALL_DIR)
        } else {
            $base = $env:LOCALAPPDATA
            if (-not $base) { $base = [Environment]::GetFolderPath('LocalApplicationData') }
            if (-not $base) { Stop-Install 'LOCALAPPDATA is not set. Set SMALL_INSTALL_DIR to the folder to install into.' }
            $dir = Join-Path $base 'Programs\small'
        }
        $dir = [System.IO.Path]::GetFullPath($dir)
        if ($dir.Length -gt 3) { $dir = $dir.TrimEnd('\') }
        return $dir
    }

    # $true when the folder is missing, empty or created by this installer: only then may it be filled or deleted.
    function Test-OwnedDir([string]$Dir) {
        if (-not (Test-Path -LiteralPath $Dir)) { return $true }
        if (-not (Test-Path -LiteralPath $Dir -PathType Container)) { return $false }
        if (Test-Path -LiteralPath (Join-Path $Dir $MarkerName) -PathType Leaf) { return $true }
        return (-not (Get-ChildItem -LiteralPath $Dir -Force | Select-Object -First 1))
    }

    # ASCII, CRLF, no BOM. An identical file is left alone: cmd.exe reads a running .cmd by offset.
    function Write-AsciiFile([string]$Path, [string[]]$Lines) {
        $bytes = [System.Text.Encoding]::ASCII.GetBytes(($Lines -join "`r`n") + "`r`n")
        if ([System.IO.File]::Exists($Path)) {
            $old = [System.IO.File]::ReadAllBytes($Path)
            if ([Convert]::ToBase64String($old) -eq [Convert]::ToBase64String($bytes)) { return }
        }
        [System.IO.File]::WriteAllBytes($Path, $bytes)
    }

    function Remove-Tree([string]$Path) {
        for ($attempt = 1; ; $attempt++) {
            if (-not (Test-Path -LiteralPath $Path)) { return }
            try {
                Remove-Item -LiteralPath $Path -Recurse -Force
                return
            } catch {
                # Antivirus scanners briefly hold freshly written files; a running "small" holds python.exe.
                if ($attempt -ge 3) {
                    Stop-Install ("Could not delete {0}: {1} Close running small commands and try again." `
                                  -f $Path, $_.Exception.Message)
                }
                Start-Sleep -Seconds 1
            }
        }
    }

    # --- native commands ----------------------------------------------------------------------------------------

    # Runs a program with its output going straight to the console; the caller checks $LASTEXITCODE. The local
    # 'Continue' keeps Windows PowerShell 5.1 hosts that capture stderr (ISE, VS Code) from turning pip warnings
    # into terminating errors.
    function Invoke-Native([string]$Exe, [string[]]$Arguments = @()) {
        $ErrorActionPreference = 'Continue'
        try {
            & $Exe @Arguments
        } catch {
            Write-Host $_.Exception.Message -ForegroundColor Red
            $global:LASTEXITCODE = 1
        }
    }

    # Asks the interpreter for its version instead of trusting the file name. The Microsoft Store alias
    # (WindowsApps\python.exe when Python is not installed) prints "Python was not found" to stderr and exits
    # with 9009 instead, so it yields $null, as does anything that is not Python 3 or does not start.
    function Get-PythonVersion([string]$Exe, [string[]]$PreArgs = @()) {
        $ErrorActionPreference = 'Continue'
        $probe = "import sys; print('SMALLPROBE', sys.version_info[0], sys.version_info[1])"
        try {
            $output = @(& $Exe @PreArgs -I -c $probe 2>$null)
        } catch {
            return $null
        }
        if ($LASTEXITCODE -ne 0) { return $null }
        foreach ($line in $output) {
            if ("$line" -match '^SMALLPROBE (\d+) (\d+)\s*$') { return [version]('{0}.{1}' -f $Matches[1], $Matches[2]) }
        }
        return $null
    }

    # Python 3.9+: the py launcher (py -3) first, then every python on PATH in PATH order.
    function Find-Python {
        $candidates = @()
        foreach ($path in @(Get-Command py -CommandType Application -All -ErrorAction SilentlyContinue |
                            ForEach-Object { $_.Path } | Select-Object -Unique)) {
            $candidates += @{ Exe = $path; PreArgs = @('-3'); Label = 'py -3' }
        }
        foreach ($path in @(Get-Command python -CommandType Application -All -ErrorAction SilentlyContinue |
                            ForEach-Object { $_.Path } | Select-Object -Unique)) {
            $candidates += @{ Exe = $path; PreArgs = @(); Label = $path }
        }
        $notes = @()
        foreach ($candidate in $candidates) {
            $version = Get-PythonVersion $candidate.Exe $candidate.PreArgs
            if ($null -eq $version) {
                if ($candidate.PreArgs.Count) {
                    $notes += '{0} -3: found no working Python 3' -f $candidate.Exe
                } elseif ($candidate.Exe -like '*\Microsoft\WindowsApps\*') {
                    $notes += '{0}: the Microsoft Store alias, not an installed Python' -f $candidate.Exe
                } else {
                    $notes += '{0}: does not start or is not Python 3' -f $candidate.Exe
                }
                continue
            }
            if ($version -lt $MinPython) {
                $notes += '{0}: Python {1} is too old' -f $candidate.Exe, $version
                continue
            }
            $candidate.Version = $version
            return $candidate
        }
        $lines = @('Python {0} or newer was not found.' -f $MinPython)
        if ($notes) { $lines += @('Checked:') + @($notes | ForEach-Object { '  ' + $_ }) }
        $lines += 'Install Python from https://www.python.org/downloads/ , open a new PowerShell window and run this command again.'
        Stop-Install ($lines -join [Environment]::NewLine)
    }

    # "small --version" through the shim, i.e. the way the user will run it.
    function Get-SmallVersion([string]$Shim) {
        $ErrorActionPreference = 'Continue'
        $output = @(& $Shim '--version' 2>&1)
        $code = $LASTEXITCODE
        $text = (@($output | ForEach-Object { "$_" }) -join ' ').Trim()
        if ($code -ne 0) { Stop-Install ('{0} --version failed (exit code {1}): {2}' -f $Shim, $code, $text) }
        return $text
    }

    # --- PATH ---------------------------------------------------------------------------------------------------

    function Test-SamePath([string]$Entry, [string]$Dir) {
        $path = [Environment]::ExpandEnvironmentVariables($Entry.Trim().Trim('"')).Trim()
        if ($path.Length -gt 3) { $path = $path.TrimEnd('\') }
        return ($path -ieq $Dir)
    }

    function Test-PathContains([string]$PathValue, [string]$Dir) {
        return [bool](@($PathValue -split ';' | Where-Object { Test-SamePath $_ $Dir }).Count)
    }

    # Removes $Dir and keeps every other entry exactly as it was (including %VARIABLES%).
    function Remove-PathEntry([string]$PathValue, [string]$Dir) {
        return (@($PathValue -split ';' | Where-Object { -not (Test-SamePath $_ $Dir) }) -join ';')
    }

    # $Dir as the first entry, any other copy of it removed.
    function Add-PathEntryFirst([string]$PathValue, [string]$Dir) {
        $rest = Remove-PathEntry $PathValue $Dir
        if ($rest) { return $Dir + ';' + $rest }
        return $Dir
    }

    # The first file that "small" resolves to in this PATH value, the way Windows searches: folder by folder,
    # and in each folder by the PATHEXT extensions.
    function Find-CommandOnPath([string]$Name, [string]$PathValue) {
        $pathext = $env:PATHEXT
        if (-not $pathext) { $pathext = '.COM;.EXE;.BAT;.CMD' }
        $extensions = @($pathext -split ';' | Where-Object { $_ })
        foreach ($entry in ($PathValue -split ';')) {
            $dir = [Environment]::ExpandEnvironmentVariables($entry.Trim().Trim('"')).Trim()
            if (-not $dir) { continue }
            foreach ($extension in $extensions) {
                try { $file = [System.IO.Path]::Combine($dir, $Name + $extension.ToLowerInvariant()) } catch { continue }
                if ([System.IO.File]::Exists($file)) { return $file }
            }
        }
        return $null
    }

    function Test-SameFile([string]$A, [string]$B) {
        return ([System.IO.Path]::GetFullPath($A) -ieq [System.IO.Path]::GetFullPath($B))
    }

    # The user PATH is read and written in the registry directly, not with [Environment]::GetEnvironmentVariable /
    # SetEnvironmentVariable: those expand entries such as %USERPROFILE%\... or %JAVA_HOME%\bin for good and turn
    # the REG_EXPAND_SZ value into REG_SZ. Here every other entry and the value type stay exactly as they were.
    function Get-UserPath {
        $result = @{ Value = ''; Kind = [Microsoft.Win32.RegistryValueKind]::ExpandString }
        $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment')
        if ($null -eq $key) { return $result }
        try {
            $value = $key.GetValue('Path', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
            if ($null -eq $value) { return $result }
            $kind = $key.GetValueKind('Path')
            if ($kind -ne [Microsoft.Win32.RegistryValueKind]::String -and
                $kind -ne [Microsoft.Win32.RegistryValueKind]::ExpandString) {
                Stop-Install ('The user PATH has an unexpected registry type ({0}); it was not changed.' -f $kind)
            }
            $result.Value = [string]$value
            $result.Kind = $kind
            return $result
        } finally {
            $key.Close()
        }
    }

    function Set-UserPath([string]$Value, [Microsoft.Win32.RegistryValueKind]$Kind) {
        $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Environment')
        try {
            if ($Value) { $key.SetValue('Path', $Value, $Kind) } else { $key.DeleteValue('Path', $false) }
        } finally {
            $key.Close()
        }
        # Tell Explorer that the environment changed (WM_SETTINGCHANGE), so that new terminals get the new PATH.
        # SetEnvironmentVariable always sends this broadcast; deleting a variable that does not exist changes
        # nothing else.
        [Environment]::SetEnvironmentVariable('SMALL_INSTALLER_NOTIFY', $null, 'User')
    }

    # bin must come BEFORE any folder with another "small" in it. Windows runs the first match on PATH, so if
    # pip's Scripts folder came first, its small.exe (left by an earlier "pip install small-software" and blocked
    # by Smart App Control) would win and "small" would keep failing with "Permission denied".
    function Add-ToPath([string]$Bin, [string]$Shim) {
        $userPath = Get-UserPath
        $firstInUser = Find-CommandOnPath 'small' $userPath.Value
        if (-not (Test-PathContains $userPath.Value $Bin)) {
            Set-UserPath (Add-PathEntryFirst $userPath.Value $Bin) $userPath.Kind
            Write-Step "Added $Bin to the front of the user PATH"
        } elseif ($firstInUser -and -not (Test-SameFile $firstInUser $Shim)) {
            Set-UserPath (Add-PathEntryFirst $userPath.Value $Bin) $userPath.Kind
            Write-Step "Moved $Bin to the front of the user PATH: $firstInUser came before it"
        } else {
            Write-Step "$Bin is already on the user PATH"
        }

        # This window: small works right away (in "irm | iex"; a script run with -File has its own environment).
        $env:Path = Add-PathEntryFirst "$env:Path" $Bin

        # New windows get the system PATH first and the user PATH after it. A "small" in a system PATH folder
        # (Python installed for all users) still wins, and fixing that needs the user (and maybe an administrator).
        $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
        $first = Find-CommandOnPath 'small' ('{0};{1}' -f $machinePath, (Get-UserPath).Value)
        if ($first -and -not (Test-SameFile $first $Shim)) {
            Write-Warn "new terminal windows will run $first instead of small.cmd."
            Write-Info 'It is on the system PATH, which Windows puts before the user PATH. It is probably the small.exe'
            Write-Info 'from "pip install small-software", which Smart App Control blocks. Uninstall that copy:'
            $scripts = Split-Path -Parent $first
            $owner = Join-Path (Split-Path -Parent $scripts) 'python.exe'
            if ((Split-Path -Leaf $scripts) -ieq 'Scripts' -and (Test-Path -LiteralPath $owner -PathType Leaf)) {
                Write-Info ('    & "{0}" -m pip uninstall small-software' -f $owner)
            } else {
                Write-Info '    python -m pip uninstall small-software    (with the Python that installed it)'
            }
            Write-Info '(from a terminal opened as administrator if that Python is installed for all users).'
            Write-Info ('Until then run small as: & "{0}"' -f $Shim)
        }
    }

    function Remove-FromPath([string]$Bin) {
        $userPath = Get-UserPath
        if (Test-PathContains $userPath.Value $Bin) {
            Set-UserPath (Remove-PathEntry $userPath.Value $Bin) $userPath.Kind
            Write-Step "Removed $Bin from the user PATH"
        }
        if (Test-PathContains "$env:Path" $Bin) { $env:Path = Remove-PathEntry "$env:Path" $Bin }
    }

    # --- install / uninstall ------------------------------------------------------------------------------------

    function Install-Small {
        $dir = Get-InstallDir
        $venv = Join-Path $dir 'venv'
        $bin = Join-Path $dir 'bin'
        $venvPython = Join-Path $venv 'Scripts\python.exe'
        $shim = Join-Path $bin 'small.cmd'
        $package = 'small-software'
        if ($env:SMALL_PACKAGE) { $package = $env:SMALL_PACKAGE }

        Write-Step "Installing small into $dir"
        if (-not (Test-OwnedDir $dir)) {
            Stop-Install (('{0} already exists and was not created by this installer (there is no {1} in it). ' +
                           'Remove it or set SMALL_INSTALL_DIR to another folder.') -f $dir, $MarkerName)
        }
        $version = $null
        if (Test-Path -LiteralPath $venvPython -PathType Leaf) { $version = Get-PythonVersion $venvPython }
        $reuse = $null -ne $version -and $version -ge $MinPython
        $python = $null
        if (-not $reuse) {
            Write-Step "Looking for Python $MinPython or newer"
            $python = Find-Python  # before anything is created: no Python, no leftovers
        }

        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        Write-AsciiFile (Join-Path $dir $MarkerName) $MarkerLines

        if ($reuse) {
            Write-Step "Using the existing environment (Python $version)"
        } else {
            if (Test-Path -LiteralPath $venv) {
                Write-Info 'The existing environment does not start (was its Python removed?); creating it again.'
                Remove-Tree $venv
            }
            Write-Step ('Creating the environment with Python {0} ({1})' -f $python.Version, $python.Label)
            Invoke-Native $python.Exe ($python.PreArgs + @('-I', '-m', 'venv', $venv))
            if ($LASTEXITCODE -ne 0) {
                Stop-Install ('Python could not create the environment in {0} (exit code {1}).' -f $venv, $LASTEXITCODE)
            }
            $version = Get-PythonVersion $venvPython
            if ($null -eq $version) { Stop-Install "$venvPython does not start." }
        }

        Write-Step ('Installing {0} with pip' -f (Hide-Secret $package))
        Invoke-Native $venvPython @('-I', '-m', 'pip', 'install', '--upgrade', '--quiet', '--disable-pip-version-check', 'pip')
        if ($LASTEXITCODE -ne 0) { Write-Warn "could not upgrade pip (exit code $LASTEXITCODE); going on with the current one." }
        Invoke-Native $venvPython @('-I', '-m', 'pip', 'install', '--upgrade', '--disable-pip-version-check', $package)
        if ($LASTEXITCODE -ne 0) {
            Stop-Install ('pip could not install {0} (exit code {1}); see its output above.' -f (Hide-Secret $package), $LASTEXITCODE)
        }

        New-Item -ItemType Directory -Force -Path $bin | Out-Null
        Write-AsciiFile $shim $ShimLines
        $installed = Get-SmallVersion $shim

        $pathChanged = $env:SMALL_NO_PATH -ne '1'
        if ($pathChanged) {
            Add-ToPath $bin $shim
        } else {
            Write-Step 'PATH was not changed (SMALL_NO_PATH=1)'
        }

        Write-Host ''
        Write-Host "$installed is installed: $shim" -ForegroundColor Green
        if (-not $pathChanged) {
            Write-Host ('Run it as: & "{0}" <command>' -f $shim)
        } elseif ($RunAsFile) {
            Write-Host 'Open a new terminal window to use the "small" command.'
        } else {
            Write-Host '"small" works in this window now. Windows that were already open need a restart.'
        }
        Write-Host ''
        Write-Host 'Next steps:'
        Write-Host '  1. Create a Railway account or workspace token (a project token cannot create projects):'
        Write-Host '       https://railway.com/account/tokens'
        Write-Host '  2. Save it to RAILWAY_API_TOKEN, replacing YOUR_TOKEN:'
        Write-Host '       [Environment]::SetEnvironmentVariable("RAILWAY_API_TOKEN", "YOUR_TOKEN", "User")  # new windows'
        Write-Host '       $env:RAILWAY_API_TOKEN = "YOUR_TOKEN"                                          # this window'
        Write-Host '  3. Create an app and deploy it:'
        Write-Host '       small init team-budget'
        Write-Host '       cd team-budget'
        Write-Host '       small deploy'
        Write-Host ''
        Write-Host 'Upgrade: run the install command again.'
        Write-Host 'Uninstall:'
        if ($RunAsFile) { Write-Host ('  powershell -ExecutionPolicy Bypass -File "{0}" -Uninstall' -f $ScriptPath) }
        Write-Host ('  & ([scriptblock]::Create((irm {0}))) -Uninstall' -f $ScriptUrl)
    }

    function Uninstall-Small {
        $dir = Get-InstallDir
        $bin = Join-Path $dir 'bin'

        Write-Step "Uninstalling small from $dir"
        if (-not (Test-OwnedDir $dir)) {
            Stop-Install ('{0} was not created by this installer (there is no {1} in it); nothing was deleted.' -f $dir, $MarkerName)
        }

        if ($env:SMALL_NO_PATH -eq '1') {
            Write-Step 'PATH was not changed (SMALL_NO_PATH=1)'
        } else {
            Remove-FromPath $bin
        }

        if (Test-Path -LiteralPath $dir) {
            # Only what the installer created; the marker goes last, so an interrupted uninstall can be re-run.
            foreach ($name in @('bin', 'venv', $MarkerName)) { Remove-Tree (Join-Path $dir $name) }
            if (Get-ChildItem -LiteralPath $dir -Force | Select-Object -First 1) {
                Write-Warn "$dir contains other files and was left in place."
            } else {
                Remove-Item -LiteralPath $dir -Force
            }
            Write-Host 'small is uninstalled.' -ForegroundColor Green
        } else {
            Write-Host "small is not installed in $dir; there was nothing to delete." -ForegroundColor Green
        }
        Write-Host 'Your apps, their small.json files and your Railway projects were not touched.'
    }

    $exitCode = 0
    try {
        if ($Uninstall) { Uninstall-Small } else { Install-Small }
    } catch {
        $exitCode = 1
        Write-Host ''
        Write-Host ('Error: {0}' -f $_.Exception.Message) -ForegroundColor Red
        if (-not $_.Exception.Data.Contains('small')) {
            Write-Host $_.InvocationInfo.PositionMessage -ForegroundColor Red
            Write-Host "This looks like a bug in the installer. Please report it: $IssuesUrl" -ForegroundColor Red
        }
    }
    if ($exitCode -ne 0 -and $RunAsFile) { exit $exitCode }
} $Uninstall.IsPresent $PSCommandPath
