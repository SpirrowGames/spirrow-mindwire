# Regression guard for the sweep's TOML basic-string handling (T-ephemeral-impl-clone-slot-per-thread
# A7, Bohr msg-10066; the msg-10049 production failure).
#
# 1. deploy/lib/TomlString.ps1: the decoder accepts exactly TOML 1.0's escapes (including \u / \U)
#    and refuses undefined escapes, surrogates, stray quotes and raw control characters; the
#    encoder escapes \ and " and refuses control characters.
# 2. Set-TomlValue, lifted from deploy/run-conductor-scheduled.ps1 and run on a real file: a
#    backslash slot path is written escaped, a second write of the same value leaves the bytes
#    unchanged, every other line stays byte-for-byte, a control character is refused, and an
#    existing line that does not decode is refused.
# 3. Get-SlotModeConfig returns the decoded slots_root.
# That the written file is read back by tomllib and the settings loader (preflight check=config) is
# tests/test_slot_mode_toml_preflight.py.
$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $repoRoot 'deploy/lib/TomlString.ps1')
. (Join-Path $repoRoot 'deploy/lib/CloneSlot.ps1')

$sweepScript = Join-Path $repoRoot 'deploy/run-conductor-scheduled.ps1'
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($sweepScript, [ref]$null, [ref]$parseErrors)
if ($parseErrors) { throw "deploy/run-conductor-scheduled.ps1 does not parse" }
$fn = $ast.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Set-TomlValue' }, $true) |
    Select-Object -First 1
if (-not $fn) { throw "function not found in sweep script: Set-TomlValue" }
Invoke-Expression $fn.Extent.Text
$script:logLines = @()
function Write-Log { param([string]$Message) $script:logLines += $Message }

$script:failures = 0
function Check {
    param([string]$Name, [bool]$Condition, [string]$Detail = '')
    if ($Condition) { Write-Host ("  PASS  {0}" -f $Name) }
    else { $script:failures++; Write-Host ("  FAIL  {0} {1}" -f $Name, $Detail) }
}
function Throws {
    param([scriptblock]$Block)
    try { & $Block | Out-Null; return $false } catch { return $true }
}

# --- 1. decoder / encoder -------------------------------------------------------------------------
$bs = [string][char]0x5C
Check 'plain text decodes unchanged' ((ConvertFrom-TomlBasicString -Text 'C:/workspace/x') -ceq 'C:/workspace/x')
Check 'empty string decodes to empty' ((ConvertFrom-TomlBasicString -Text '') -ceq '')
Check '\\ and \" decode' ((ConvertFrom-TomlBasicString -Text ('a' + $bs + $bs + 'b' + $bs + '"c')) -ceq ('a' + $bs + 'b"c'))
$ctl = ConvertFrom-TomlBasicString -Text ($bs + 'b' + $bs + 't' + $bs + 'n' + $bs + 'f' + $bs + 'r')
Check '\b \t \n \f \r decode' ($ctl -ceq ([string][char]8 + [char]9 + [char]10 + [char]12 + [char]13))
Check '\u00e9 decodes' ((ConvertFrom-TomlBasicString -Text ('caf' + $bs + 'u00e9')) -ceq ('caf' + [char]0xE9))
Check '\U0001F600 decodes to a surrogate pair' ((ConvertFrom-TomlBasicString -Text ($bs + 'U0001F600')) -ceq [char]::ConvertFromUtf32(0x1F600))
Check 'a raw tab is allowed' ((ConvertFrom-TomlBasicString -Text "a`tb") -ceq "a`tb")
Check 'undefined escape \w refused (the msg-10049 line)' (Throws { ConvertFrom-TomlBasicString -Text ('C:' + $bs + 'workspace') })
Check 'TOML 1.1 \e refused (tomllib reads 1.0)' (Throws { ConvertFrom-TomlBasicString -Text ($bs + 'e') })
Check 'trailing lone \ refused' (Throws { ConvertFrom-TomlBasicString -Text ('abc' + $bs) })
Check 'short \u refused' (Throws { ConvertFrom-TomlBasicString -Text ($bs + 'u12') })
Check 'non-hex \u refused' (Throws { ConvertFrom-TomlBasicString -Text ($bs + 'u12G4') })
Check 'surrogate \uD800 refused' (Throws { ConvertFrom-TomlBasicString -Text ($bs + 'uD800') })
Check 'beyond U+10FFFF refused' (Throws { ConvertFrom-TomlBasicString -Text ($bs + 'U00110000') })
Check 'unescaped " refused' (Throws { ConvertFrom-TomlBasicString -Text 'a"b' })
Check 'raw newline refused' (Throws { ConvertFrom-TomlBasicString -Text "a`nb" })
Check 'raw DEL refused' (Throws { ConvertFrom-TomlBasicString -Text ('a' + [char]0x7F) })
Check 'encoder escapes \ and "' ((ConvertTo-TomlBasicString -Value ('C:' + $bs + 'a"b')) -ceq ('C:' + $bs + $bs + 'a' + $bs + '"b'))
Check 'encoder refuses a control character' (Throws { ConvertTo-TomlBasicString -Value "a`nb" })
Check 'encoder refuses a tab' (Throws { ConvertTo-TomlBasicString -Value "a`tb" })
$rt = 'C:' + $bs + 'workspace' + $bs + 'slots' + $bs + 'mindwire-impl' + $bs + 'slot-01'
Check 'encode then decode round-trips a slot path' ((ConvertFrom-TomlBasicString -Text (ConvertTo-TomlBasicString -Value $rt)) -ceq $rt)

$tmp = Join-Path $repoRoot ".git/mindwire-scratch/test-tomlstring-$PID"
New-Item -ItemType Directory -Path $tmp -Force | Out-Null
$utf8 = New-Object System.Text.UTF8Encoding($false)
try {
    # --- 2. Set-TomlValue on a real file ----------------------------------------------------------
    $cfg = Join-Path $tmp 'mindwire.toml'
    $base = "# header`n[loop]`nproject = `"p`"`nrepo_dir = `"C:/workspace/sandbox/mindwire-impl`"  `n# keep`n[conductor]`ntask_thread_id = `"T-x`"`n[other]`nrepo_dir = `"untouched`"`n"
    [System.IO.File]::WriteAllText($cfg, $base, $utf8)
    Set-TomlValue -Path $cfg -Section 'loop' -Key 'repo_dir' -Value $rt
    $after = [System.IO.File]::ReadAllText($cfg)
    $want = $base.Replace("repo_dir = `"C:/workspace/sandbox/mindwire-impl`"  ", 'repo_dir = "' + $rt.Replace($bs, $bs + $bs) + '"')
    Check 'backslash slot path is written escaped; every other line byte-for-byte' ($after -ceq $want) "got: $after"
    $bytes1 = [System.IO.File]::ReadAllBytes($cfg)
    $script:logLines = @()
    Set-TomlValue -Path $cfg -Section 'loop' -Key 'repo_dir' -Value $rt
    $bytes2 = [System.IO.File]::ReadAllBytes($cfg)
    Check 'second write of the same value leaves the bytes unchanged (decoded compare)' (
        ([Convert]::ToBase64String($bytes1) -ceq [Convert]::ToBase64String($bytes2)) -and $script:logLines.Count -eq 0)

    $quoted = 'T-"quoted"' + $bs
    Set-TomlValue -Path $cfg -Section 'conductor' -Key 'task_thread_id' -Value $quoted
    $line = ([System.IO.File]::ReadAllLines($cfg) | Where-Object { $_ -like 'task_thread_id*' })
    Check 'a value with " and \ is written escaped' ($line -ceq ('task_thread_id = "T-' + $bs + '"quoted' + $bs + '"' + $bs + $bs + '"')) "got: $line"
    $m = [regex]::Match($line, '^\s*task_thread_id\s*=\s*"(' + $TomlBasicStringBodyPattern + ')"\s*$')
    Check '... and decodes back to the same value' ($m.Success -and (ConvertFrom-TomlBasicString -Text $m.Groups[1].Value) -ceq $quoted)

    $before = [System.IO.File]::ReadAllText($cfg)
    Check 'a control character is refused' (Throws { Set-TomlValue -Path $cfg -Section 'loop' -Key 'project' -Value "a`nb" })
    Check '... and the file is unchanged' ([System.IO.File]::ReadAllText($cfg) -ceq $before)

    [System.IO.File]::WriteAllText($cfg, "[loop]`nrepo_dir = `"C:\workspace\slots\x`"`n", $utf8)
    Check 'an existing line that does not decode (the msg-10049 file) is refused' (Throws { Set-TomlValue -Path $cfg -Section 'loop' -Key 'repo_dir' -Value 'C:/r' })
    Check 'missing key still refused' (Throws { Set-TomlValue -Path $cfg -Section 'loop' -Key 'project' -Value 'p' })

    # --- 3. Get-SlotModeConfig decodes slots_root -------------------------------------------------
    [System.IO.File]::WriteAllText($cfg, "[loop]`nslots_root = `"C:\\workspace\\slots`"  # pool`n", $utf8)
    $sm = Get-SlotModeConfig -ConfigPath $cfg
    Check 'escaped slots_root is decoded' ($sm.slots_root -ceq ('C:' + $bs + 'workspace' + $bs + 'slots')) "got: $($sm.slots_root)"
    [System.IO.File]::WriteAllText($cfg, "[loop]`nslots_root = `"C:/workspace/slots`"`n", $utf8)
    Check 'forward-slash slots_root unchanged' ((Get-SlotModeConfig -ConfigPath $cfg).slots_root -ceq 'C:/workspace/slots')
    [System.IO.File]::WriteAllText($cfg, "[loop]`nslots_root = `"C:\workspace\slots`"`n", $utf8)
    Check 'slots_root with an undefined escape is refused, not read raw' (Throws { Get-SlotModeConfig -ConfigPath $cfg })
    # A7 round 1 (PR-gate on 97ac37e): a trailing lone backslash eats the closing quote; the line must
    # be refused, not skipped as if slots_root were absent (which would mean shared-clone mode).
    [System.IO.File]::WriteAllText($cfg, "[loop]`nslots_root = `"C:/slots\`"`n", $utf8)
    Check 'slots_root with a trailing lone backslash is refused, not treated as absent' (Throws { Get-SlotModeConfig -ConfigPath $cfg })
    [System.IO.File]::WriteAllText($cfg, "[loop]`nslots_root = 'C:/slots'`n", $utf8)
    Check 'slots_root that is not a basic string is refused, not treated as absent' (Throws { Get-SlotModeConfig -ConfigPath $cfg })
    [System.IO.File]::WriteAllText($cfg, "[loop]`nslots_root = `"`"`n", $utf8)
    Check 'empty slots_root is still the shared-clone path' ($null -eq (Get-SlotModeConfig -ConfigPath $cfg))
    [System.IO.File]::WriteAllText($cfg, "[loop]`n# slots_root = `"C:/slots\`"`n", $utf8)
    Check 'a commented-out slots_root is still absent' ($null -eq (Get-SlotModeConfig -ConfigPath $cfg))
}
finally { Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue }

if ($script:failures -gt 0) { Write-Host "FAILED: $($script:failures)"; exit 1 }
Write-Host "Test-TomlString: all passed"
