# deploy/lib/TomlString.ps1 — TOML basic strings ("...") for the sweep's line-oriented config
# reader and writer (T-ephemeral-impl-clone-slot-per-thread A7, Bohr msg-10066; msg-10049).
#
# The sweep reads and writes mindwire.toml line by line (Set-TomlValue, Get-SlotModeConfig) rather
# than through a TOML library, so the string escaping has to be done here. Before A7 the writer put
# the raw value between quotes: a slot path from slot-prep (C:\workspace\slots\...) became
# `repo_dir = "C:\workspace\..."`, which TOML rejects (`\w` is not an escape), and every role turn
# in slot mode was refused at preflight check=config.
#
# The contract is TOML 1.0's basic string, the version Python's tomllib (the settings loader) reads:
#   escapes  \b \t \n \f \r \" \\ \uXXXX \UXXXXXXXX (a Unicode scalar value, so no surrogates);
#   any other escape, an unescaped `"`, a trailing lone `\`, and a raw control character other than
#   tab are invalid and refused — never guessed at.
# The writer never emits a control character: it refuses a value that contains one rather than
# escaping it (none of the values the sweep writes — project, repo_dir, task_thread_id — can).

# The quoted part of a `key = "..."` line: a basic string body, escapes kept as written. Shared by
# the reader and the writer so both pick the same characters off the line.
$TomlBasicStringBodyPattern = '(?:[^"\\]|\\.)*'

function ConvertFrom-TomlBasicString {
    param([AllowEmptyString()][string]$Text)
    $sb = New-Object System.Text.StringBuilder
    $i = 0
    while ($i -lt $Text.Length) {
        $c = $Text[$i]
        if ($c -eq '"') { throw "invalid TOML basic string: unescaped '`"' at offset $i" }
        if ($c -eq '\') {
            if ($i + 1 -ge $Text.Length) { throw "invalid TOML basic string: trailing '\'" }
            $e = $Text[$i + 1]
            switch -CaseSensitive ([string]$e) {
                'b' { [void]$sb.Append([char]0x08); $i += 2; continue }
                't' { [void]$sb.Append([char]0x09); $i += 2; continue }
                'n' { [void]$sb.Append([char]0x0A); $i += 2; continue }
                'f' { [void]$sb.Append([char]0x0C); $i += 2; continue }
                'r' { [void]$sb.Append([char]0x0D); $i += 2; continue }
                '"' { [void]$sb.Append('"'); $i += 2; continue }
                '\' { [void]$sb.Append('\'); $i += 2; continue }
                { $_ -ceq 'u' -or $_ -ceq 'U' } {
                    $n = if ($_ -ceq 'u') { 4 } else { 8 }
                    if ($i + 2 + $n -gt $Text.Length) { throw "invalid TOML basic string: short \$_ escape at offset $i" }
                    $hex = $Text.Substring($i + 2, $n)
                    if ($hex -notmatch '^[0-9A-Fa-f]+$') { throw "invalid TOML basic string: bad \$_ escape '$hex' at offset $i" }
                    $cp = [Convert]::ToInt64($hex, 16)
                    if ($cp -gt 0x10FFFF -or ($cp -ge 0xD800 -and $cp -le 0xDFFF)) {
                        throw "invalid TOML basic string: \$_$hex is not a Unicode scalar value"
                    }
                    [void]$sb.Append([char]::ConvertFromUtf32([int]$cp))
                    $i += 2 + $n
                    continue
                }
                default { throw "invalid TOML basic string: undefined escape '\$e' at offset $i" }
            }
            continue
        }
        $code = [int]$c
        if (($code -lt 0x20 -and $code -ne 0x09) -or $code -eq 0x7F) {
            throw ("invalid TOML basic string: raw control character U+{0:X4} at offset {1}" -f $code, $i)
        }
        [void]$sb.Append($c)
        $i++
    }
    return $sb.ToString()
}

function ConvertTo-TomlBasicString {
    param([AllowEmptyString()][string]$Value)
    foreach ($ch in $Value.ToCharArray()) {
        $code = [int]$ch
        if ($code -lt 0x20 -or $code -eq 0x7F) {
            throw ("refusing to write a TOML value with a control character (U+{0:X4})" -f $code)
        }
    }
    return $Value.Replace('\', '\\').Replace('"', '\"')
}
