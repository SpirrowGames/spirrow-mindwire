# deploy/lib/ProjectLiveness.ps1 — the per-project liveness view for the daily digest
# (T-digest-lacks-project-liveness-view).
#
# Why this exists. "Has magickit finished its planned work, or has it stopped?" had no answer
# short of measuring every project by hand (msg-2015): magickit had 10 open threads and the loop
# reached 2 of them, and nothing outside showed it. Everything needed is already observed; it is
# scattered. This file joins it into one row per project.
#
# Design: Bohr msg-2191 .. msg-2203 (v1 .. v7), implementation plan msg-8380, amended by msg-8382
# (R-10 snapshot), msg-8384 (R-11 follow-up) and msg-8386 (R-12: the omitted rows are sent in full,
# split into numbered follow-ups, and no file path appears in Discord text). Human go: msg-8485.
#
# PURE FUNCTIONS ONLY. No I/O, no clock reads, no network: every input is a parameter, so the
# caller builds the facts once per tick and the log line, the digest section, the follow-ups and
# the snapshot all read the same values (the "two mechanisms read different values" error this
# design hit four times, msg-2203 §1).
#
# Pipeline (each stage reads only the previous stage's output):
#   ConvertTo-LivenessThreadClass   the ONLY place thread.status is interpreted (msg-8380 §2-3)
#   Get-ProjectLivenessFacts        join threads / sweep membership / nominations / open PRs; R-9
#                                   age normalisation happens here, once
#   Get-ProjectLivenessState        判定表 v3, first match wins, `tracked_open > 0` in each predicate
#   Get-ProjectLivenessTier         R-8: tier = max(state_tier, signal_tier...), closure rule
#   Get-ProjectLivenessDigestLines  L0 (<=140) / L1 (<=400), attention order, tail-only omission
#   Get-ProjectLivenessContinuationChunks   R-12 numbered follow-ups carrying every omitted row
#   ConvertTo-ProjectLivenessSnapshot       R-10 full snapshot (future L2 content; never linked)

# Knobs (msg-2203 §6: exactly two).
$LivenessDStallDays = 3.0
$LivenessDHumanDays = 7.0
# R-9: a missing age contributes ratio 1 + delta — "over, by the least amount".
$LivenessRatioDelta = 0.01
$LivenessL0Budget = 140
$LivenessL1Budget = 400
# R-12: a minimal-form row must fit well inside one follow-up; asserted at the lib boundary.
$LivenessMaxMinimalRowLength = 200

# R-8 tables. Each entry is the (threshold, under-threshold tier, over-threshold tier) triple the
# closure rule requires (msg-2201 §3). $null for a tier means "unreachable by definition"; reaching
# it is a contradiction and throws. A state or signal with no entry throws too: a new one cannot be
# added without writing its triple here.
$LivenessStateTier = @{
    'UNTRACKED' = @{ threshold = 'stall'; under = 2;     over = 3 }
    'STALLED'   = @{ threshold = 'stall'; under = $null; over = 3 }
    'RUNNING'   = @{ threshold = 'stall'; under = 2;     over = $null }
    'HUMAN'     = @{ threshold = 'human'; under = 2;     over = 3 }
    'SHIP'      = @{ threshold = 'human'; under = 2;     over = 3 }
    'PARKED'    = @{ threshold = $null;   under = 2;     over = 2 }
    'DONE'      = @{ threshold = $null;   under = 0;     over = 0 }
    'EMPTY'     = @{ threshold = $null;   under = 0;     over = 0 }
}
$LivenessSignalTier = @{
    'dark'  = @{ threshold = 'stall'; under = 2;     over = 3 }
    'stale' = @{ threshold = 'stall'; under = $null; over = 3 }
    'rot'   = @{ threshold = $null;   under = 1;     over = 1 }
}
# Attention order is by tier, so one name per tier for the omitted-row breakdown (msg-2199).
$LivenessTierName = @{ 3 = 'critical'; 2 = 'watch'; 1 = 'hygiene'; 0 = 'quiet' }
# L0 state tally abbreviations, in table order.
$LivenessStateOrder = @('UNTRACKED', 'HUMAN', 'STALLED', 'RUNNING', 'SHIP', 'PARKED', 'DONE', 'EMPTY')
$LivenessStateAbbrev = @{
    'UNTRACKED' = 'U'; 'HUMAN' = 'H'; 'STALLED' = 'St'; 'RUNNING' = 'R'
    'SHIP' = 'Sh'; 'PARKED' = 'P'; 'DONE' = 'D'; 'EMPTY' = 'E'
}

# The single reading of thread.status (msg-8380 §2-3). Everything downstream reads the class this
# returns, never the status: if status is later replaced (T-project-status-carries-no-information,
# W x D), this body is the whole change. open = active | awaiting_reply, closed = resolved |
# superseded (msg-2191 D-1). An unknown status is NOT guessed into a class: it is 'unknown', and
# the facts stage counts it as open (fail-loud: an unreadable thread must not buy DONE).
function ConvertTo-LivenessThreadClass {
    param([AllowNull()][AllowEmptyString()][string]$Status)
    switch ("$Status".Trim().ToLowerInvariant()) {
        'active'         { return 'open' }
        'awaiting_reply' { return 'open' }
        'parked'         { return 'parked' }
        'resolved'       { return 'closed' }
        'superseded'     { return 'closed' }
        default          { return 'unknown' }
    }
}

# R-9 normalisation of one timestamp into an age in days, done once at intake. Unreadable or
# missing => +Infinity with provenance 'age?'. A future timestamp is clamped to 0 and flagged
# 'clock?' (msg-2191 D-7: last_activity_at is caller-supplied, not the ordering truth).
function ConvertTo-LivenessAge {
    param($Value, [datetime]$Now)
    $s = "$Value".Trim()
    if (-not $s) { return @{ days = [double]::PositiveInfinity; flag = 'age?' } }
    $dto = [DateTimeOffset]::MinValue
    if (-not [DateTimeOffset]::TryParse($s, [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::AssumeUniversal, [ref]$dto)) {
        return @{ days = [double]::PositiveInfinity; flag = 'age?' }
    }
    $nowUtc = [DateTimeOffset]::new($Now.ToUniversalTime(), [TimeSpan]::Zero)
    $days = ($nowUtc - $dto.ToUniversalTime()).TotalDays
    if ($days -lt 0) { return @{ days = 0.0; flag = 'clock?' } }
    return @{ days = [double]$days; flag = $null }
}

# Ages summary of a set of normalised items: known max, missing count, and the aggregates the
# predicates/ratio need. Infinity propagates into max/min exactly as R-9 requires.
function Get-LivenessAgeAgg {
    param([object[]]$Items)
    $items = @($Items | Where-Object { $null -ne $_ })
    $known = @($items | Where-Object { -not [double]::IsInfinity([double]$_.age) } | ForEach-Object { [double]$_.age })
    $missing = @($items | Where-Object { [double]::IsInfinity([double]$_.age) }).Count
    $max = if ($items.Count -eq 0) { $null } elseif ($missing -gt 0) { [double]::PositiveInfinity } else { ($known | Measure-Object -Maximum).Maximum }
    $min = if ($items.Count -eq 0) { $null } elseif ($known.Count -eq 0) { [double]::PositiveInfinity } else { ($known | Measure-Object -Minimum).Minimum }
    $knownMax = if ($known.Count -gt 0) { ($known | Measure-Object -Maximum).Maximum } else { $null }
    return @{ count = $items.Count; max = $max; min = $min; known_max = $knownMax; missing = $missing }
}

# Join the inputs into one facts record per project. Inputs (all already observed by the tick —
# no new observation, msg-8380 §3):
#   -Threads       objects { project; thread_id; status; last_activity_at } (chatroom listing)
#   -SweepEntries  objects { project; thread_id } (config/sweep.json membership)
#   -Nominations   hashtable "project/thread_id" -> nomination ('human' / 'none' / persona name)
#   -OpenPrs       objects { project; created_at }
#   -Projects      extra project names to enumerate (D-6 union: sweep / chatroom / list)
#   -Unmeasured    project names whose listing failed this tick; their rows are not computed
#                  from partial data — the caller reports them (see L0 'unmeasured')
# Enumeration is the D-6 union and every row keeps its sources, so a name that exists only in
# sweep.json renders '(sweep-only)' (msg-2197).
function Get-ProjectLivenessFacts {
    param(
        [object[]]$Threads = @(),
        [object[]]$SweepEntries = @(),
        [hashtable]$Nominations = @{},
        [object[]]$OpenPrs = @(),
        [string[]]$Projects = @(),
        [string[]]$Unmeasured = @(),
        [datetime]$Now
    )
    $sources = [ordered]@{}
    function _addSrc([string]$p, [string]$src) {
        if (-not $p) { return }
        if (-not $sources.Contains($p)) { $sources[$p] = [System.Collections.Generic.List[string]]::new() }
        if (-not $sources[$p].Contains($src)) { $sources[$p].Add($src) }
    }
    foreach ($t in @($Threads)) { if ($t) { _addSrc "$($t.project)" 'chatroom' } }
    foreach ($e in @($SweepEntries)) { if ($e) { _addSrc "$($e.project)" 'sweep' } }
    foreach ($p in @($Projects)) { _addSrc "$p" 'list' }
    foreach ($pr in @($OpenPrs)) { if ($pr) { _addSrc "$($pr.project)" 'pr' } }

    $unmeasuredSet = @{}
    foreach ($u in @($Unmeasured)) { if ($u) { $unmeasuredSet["$u"] = $true } }

    $out = @()
    foreach ($proj in $sources.Keys) {
        if ($unmeasuredSet.ContainsKey($proj)) { continue }
        $inSweep = @{}
        foreach ($e in @($SweepEntries)) { if ($e -and "$($e.project)" -eq $proj) { $inSweep["$($e.thread_id)"] = $true } }
        $byId = @{}
        $open = @(); $parked = @(); $closed = 0
        foreach ($t in @($Threads)) {
            if (-not $t -or "$($t.project)" -ne $proj) { continue }
            $id = "$($t.thread_id)"
            $class = ConvertTo-LivenessThreadClass -Status "$($t.status)"
            $byId[$id] = $class
            $a = ConvertTo-LivenessAge -Value $t.last_activity_at -Now $Now
            $key = "$proj/$id"
            $nom = if ($Nominations.ContainsKey($key)) { "$($Nominations[$key])" } else { $null }
            $item = @{ thread_id = $id; age = $a.days; flag = $a.flag; tracked = $inSweep.ContainsKey($id); nomination = $nom }
            switch ($class) {
                'open'    { $open += $item }
                'unknown' { $open += $item }
                'parked'  { $parked += $item }
                'closed'  { $closed++ }
            }
        }
        # rot: a sweep entry occupying a slot for a closed thread, or pointing at no thread at all.
        $rot = 0
        foreach ($id in $inSweep.Keys) {
            if (-not $byId.ContainsKey($id) -or $byId[$id] -eq 'closed') { $rot++ }
        }
        $prs = @()
        foreach ($pr in @($OpenPrs)) {
            if ($pr -and "$($pr.project)" -eq $proj) {
                $a = ConvertTo-LivenessAge -Value $pr.created_at -Now $Now
                $prs += @{ age = $a.days; flag = $a.flag }
            }
        }
        $tracked = @($open | Where-Object { $_.tracked })
        $dark = @($open | Where-Object { -not $_.tracked })
        $stale = @($tracked | Where-Object { [double]$_.age -gt $LivenessDStallDays })
        # D-4 wait vocabulary over open threads, joined from the nomination: human -> h (tier-c),
        # persona -> r (role), none -> u (unknown: said none but neither parked nor resolved — a
        # bookkeeping-leak indicator kept on purpose), no nomination observed -> '?'.
        $wait = [ordered]@{ h = 0; r = 0; u = 0; '?' = 0 }
        foreach ($o in $open) {
            $n = "$($o.nomination)".Trim().ToLowerInvariant()
            if (-not $n) { $wait['?']++ } elseif ($n -eq 'human') { $wait['h']++ } elseif ($n -eq 'none') { $wait['u']++ } else { $wait['r']++ }
        }
        $allItems = @($open) + @($parked) + @($prs)
        $out += [PSCustomObject]@{
            name           = $proj
            sources        = @($sources[$proj])
            threads        = $byId.Count
            open           = @($open).Count
            tracked        = @($tracked)
            dark           = @($dark)
            stale          = @($stale)
            parked         = @($parked)
            closed         = $closed
            rot            = $rot
            prs            = @($prs)
            wait           = $wait
            human_all      = (@($tracked).Count -gt 0 -and @($tracked | Where-Object { "$($_.nomination)".Trim().ToLowerInvariant() -ne 'human' }).Count -eq 0)
            clock_flags    = @($allItems | Where-Object { $_.flag -eq 'clock?' }).Count
        }
    }
    return , $out
}

# 判定表 v3 (msg-2195), first match wins, with `tracked_open > 0` written INTO each tracked
# predicate (R-5: not left to evaluation order). Returns @{ state; age } where age is the aggregate
# the state's threshold is judged on (R-4: min for STALLED/RUNNING; oldest for the rest).
function Get-ProjectLivenessState {
    param($Facts)
    $f = $Facts
    $tN = @($f.tracked).Count
    $tAgg = Get-LivenessAgeAgg -Items $f.tracked
    if ($f.open -gt 0 -and $tN -eq 0) {
        return @{ state = 'UNTRACKED'; agg = (Get-LivenessAgeAgg -Items $f.dark); use = 'max' }
    }
    if ($tN -gt 0 -and $f.human_all) { return @{ state = 'HUMAN'; agg = $tAgg; use = 'max' } }
    if ($tN -gt 0 -and [double]$tAgg.min -gt $LivenessDStallDays) { return @{ state = 'STALLED'; agg = $tAgg; use = 'min' } }
    if ($tN -gt 0) { return @{ state = 'RUNNING'; agg = $tAgg; use = 'min' } }
    $prN = @($f.prs).Count
    if ($f.open -eq 0 -and $prN -gt 0) { return @{ state = 'SHIP'; agg = (Get-LivenessAgeAgg -Items $f.prs); use = 'max' } }
    if ($f.open -eq 0 -and $prN -eq 0 -and @($f.parked).Count -gt 0) { return @{ state = 'PARKED'; agg = (Get-LivenessAgeAgg -Items $f.parked); use = 'max' } }
    if ($f.open -eq 0 -and $prN -eq 0 -and @($f.parked).Count -eq 0 -and $f.closed -gt 0) { return @{ state = 'DONE'; agg = $null; use = $null } }
    if ($f.threads -eq 0 -and $prN -eq 0) { return @{ state = 'EMPTY'; agg = $null; use = $null } }
    # Unreachable when the table is total (msg-2195 検算). Throwing keeps it honest if it is not.
    throw "liveness: no state for project '$($f.name)' (table v3 is not total for this input)"
}

function Get-LivenessThresholdDays {
    param($Name)
    switch ($Name) { 'stall' { return $LivenessDStallDays } 'human' { return $LivenessDHumanDays } default { return $null } }
}

# One element's (tier, ratio) from its table triple. $Agg/$Use pick the age the threshold reads.
function Get-LivenessElementTier {
    param([hashtable]$Table, [string]$Key, $Agg, [string]$Use, [string]$Kind)
    if (-not $Table.ContainsKey($Key)) { throw "liveness: $Kind '$Key' has no tier triple (closure rule, msg-2201 §3)" }
    $row = $Table[$Key]
    $thr = Get-LivenessThresholdDays $row.threshold
    if ($null -eq $thr -or $null -eq $Agg -or [int]$Agg.count -eq 0) {
        $t = $row.under
        if ($null -eq $t) { throw "liveness: $Kind '$Key' reached its unreachable under-threshold tier" }
        return @{ tier = [int]$t; ratio = 0.0 }
    }
    $age = if ($Use -eq 'min') { [double]$Agg.min } else { [double]$Agg.max }
    $over = $age -gt $thr
    $t = if ($over) { $row.over } else { $row.under }
    if ($null -eq $t) { throw "liveness: $Kind '$Key' reached an unreachable tier (over=$over)" }
    # R-9 ratio: never fabricate an excess from a missing age. Infinity => 1 + delta, but a known
    # larger excess among the same items still counts (max over the elements).
    $ratio = if (-not [double]::IsInfinity($age)) { $age / $thr }
             else {
                 $known = if ($Use -eq 'min') { $null } else { $Agg.known_max }
                 $r = 1.0 + $LivenessRatioDelta
                 if ($null -ne $known -and ([double]$known / $thr) -gt $r) { [double]$known / $thr } else { $r }
             }
    return @{ tier = [int]$t; ratio = [double]$ratio }
}

# R-8: tier = max over {state} ∪ signals; overdue_ratio over the same set (with {0}). The state
# term is always present, so the max is never over an empty set — totality reduces to table v3.
function Get-ProjectLivenessTier {
    param($Facts, $StateResult)
    $s = Get-LivenessElementTier -Table $LivenessStateTier -Key $StateResult.state -Agg $StateResult.agg -Use $StateResult.use -Kind 'state'
    $tier = $s.tier; $ratio = [math]::Max(0.0, $s.ratio)
    $signals = @()
    if (@($Facts.dark).Count -gt 0) {
        $e = Get-LivenessElementTier -Table $LivenessSignalTier -Key 'dark' -Agg (Get-LivenessAgeAgg -Items $Facts.dark) -Use 'max' -Kind 'signal'
        $signals += @{ name = 'dark'; tier = $e.tier; ratio = $e.ratio }
    }
    if (@($Facts.stale).Count -gt 0) {
        $e = Get-LivenessElementTier -Table $LivenessSignalTier -Key 'stale' -Agg (Get-LivenessAgeAgg -Items $Facts.stale) -Use 'max' -Kind 'signal'
        $signals += @{ name = 'stale'; tier = $e.tier; ratio = $e.ratio }
    }
    if ([int]$Facts.rot -gt 0) {
        $e = Get-LivenessElementTier -Table $LivenessSignalTier -Key 'rot' -Agg $null -Use 'max' -Kind 'signal'
        $signals += @{ name = 'rot'; tier = $e.tier; ratio = $e.ratio }
    }
    foreach ($g in $signals) { if ($g.tier -gt $tier) { $tier = $g.tier }; if ($g.ratio -gt $ratio) { $ratio = $g.ratio } }
    return @{ tier = [int]$tier; ratio = [double]$ratio; signals = $signals; state_ratio = $s.ratio }
}

function Get-OverdueRatio {
    param($Facts, $StateResult)
    return (Get-ProjectLivenessTier -Facts $Facts -StateResult $StateResult).ratio
}

function Format-LivenessDays {
    param($Agg, [string]$Use = 'max', [switch]$Bare)
    if ($null -eq $Agg -or [int]$Agg.count -eq 0) { return '' }
    $v = if ($Use -eq 'min') { $Agg.min } else { $Agg.max }
    if (-not [double]::IsInfinity([double]$v)) { return ("{0}d" -f [int][math]::Floor([double]$v)) }
    # R-9 display: never mix the missing into the number; >= the known max, and the missing count.
    if ($null -ne $Agg.known_max -and $Use -ne 'min') { return ("≥{0}d" -f [int][math]::Floor([double]$Agg.known_max)) }
    return 'age?'
}

function Get-LivenessAgeNote {
    param($Agg)
    if ($null -eq $Agg -or [int]$Agg.missing -eq 0 -or $null -eq $Agg.known_max) { return '' }
    return ", $($Agg.missing) age?"
}

# One evaluated row: facts + state + tier + its rendered forms. Rendering levels (msg-2203 §7
# field deletion order): 0 full, 1 drop PR/rot detail, 2 drop wait breakdown, 3 minimal
# (`name STATE · main signal`, no stall numbers).
function Get-ProjectLivenessRow {
    param($Facts)
    $st = Get-ProjectLivenessState -Facts $Facts
    $t = Get-ProjectLivenessTier -Facts $Facts -StateResult $st
    $f = $Facts
    $token = switch ($st.state) {
        'DONE'  { 'DONE(tracked)' }
        'EMPTY' { if (@($f.sources).Count -eq 1 -and $f.sources[0] -eq 'sweep') { 'EMPTY (sweep-only)' } else { 'EMPTY' } }
        'HUMAN' {
            $d = Format-LivenessDays -Agg $st.agg
            $bang = if ([double]$st.agg.max -gt $LivenessDHumanDays) { '!' } else { '' }
            "HUMAN $d$bang"
        }
        'SHIP' {
            $d = Format-LivenessDays -Agg $st.agg
            $bang = if ([double]$st.agg.max -gt $LivenessDHumanDays) { '!' } else { '' }
            "SHIP $d$bang"
        }
        default { $st.state }
    }
    $openN = [int]$f.open
    $darkAgg = Get-LivenessAgeAgg -Items $f.dark
    $staleAgg = Get-LivenessAgeAgg -Items $f.stale
    $tAgg = Get-LivenessAgeAgg -Items $f.tracked
    $fields = [ordered]@{}
    if (@($f.dark).Count -gt 0) {
        $fields['dark'] = @{ full = ("dark {0}/{1} (oldest {2}{3})" -f @($f.dark).Count, $openN, (Format-LivenessDays $darkAgg), (Get-LivenessAgeNote $darkAgg))
                             min = ("dark {0}/{1}" -f @($f.dark).Count, $openN) }
    }
    if (@($f.stale).Count -gt 0) {
        $fields['stale'] = @{ full = ("stale {0}/{1} ({2}{3})" -f @($f.stale).Count, @($f.tracked).Count, (Format-LivenessDays $staleAgg), (Get-LivenessAgeNote $staleAgg))
                              min = ("stale {0}/{1}" -f @($f.stale).Count, @($f.tracked).Count) }
    }
    # R-9 §3: the stale count of an age-missing tracked thread is flagged in display.
    if (@($f.stale).Count -gt 0 -and $staleAgg.missing -gt 0 -and $null -eq $staleAgg.known_max) {
        $fields['stale'].full = ("stale {0}/{1} (age?)" -f @($f.stale).Count, @($f.tracked).Count)
    }
    if ($st.state -in @('STALLED', 'RUNNING') -and $null -ne $tAgg -and $tAgg.missing -gt 0 -and -not $fields.Contains('stale')) {
        $fields['agemiss'] = @{ full = "$($tAgg.missing) age?"; min = $null }
    }
    if ($st.state -eq 'PARKED') {
        $pAgg = Get-LivenessAgeAgg -Items $f.parked
        $fields['parked'] = @{ full = ("0 open / {0} parked (oldest {1}) / {2} closed" -f @($f.parked).Count, (Format-LivenessDays $pAgg), $f.closed); min = $null }
    }
    if ($st.state -eq 'DONE') {
        $fields['done'] = @{ full = ("0 open / {0} closed / 0 PR" -f $f.closed); min = $null }
    }
    $w = $f.wait
    if ($openN -gt 0) {
        $parts = @("h$($w['h'])", "r$($w['r'])", "u$($w['u'])")
        if ($w['?'] -gt 0) { $parts += "?$($w['?'])" }
        $fields['wait'] = @{ full = ("wait " + ($parts -join '/')); min = $null; level = 2 }
    }
    if ($st.state -notin @('DONE', 'PARKED', 'EMPTY')) {
        $fields['pr'] = @{ full = ("PR {0}" -f @($f.prs).Count); min = $null; level = 1 }
    }
    if ([int]$f.rot -gt 0) { $fields['rot'] = @{ full = ("rot {0}" -f $f.rot); min = ("rot {0}" -f $f.rot) } }
    if ([int]$f.clock_flags -gt 0) { $fields['clock'] = @{ full = ("{0} clock?" -f $f.clock_flags); min = $null; level = 1 } }

    $render = {
        param([int]$Level)
        $segs = @()
        foreach ($k in $fields.Keys) {
            $fd = $fields[$k]
            if ($fd.ContainsKey('level') -and $Level -ge [int]$fd.level) { continue }
            $segs += $fd.full
        }
        return (@("$($f.name) $token") + $segs) -join ' · '
    }
    # Minimal form: state token + the main signal (highest tier, then ratio) without numbers.
    $main = $null
    foreach ($g in @($t.signals | Sort-Object -Property @{ Expression = { $_.tier }; Descending = $true }, @{ Expression = { $_.ratio }; Descending = $true })) {
        if ($fields.Contains($g.name) -and $fields[$g.name].min) { $main = $fields[$g.name].min; break }
    }
    $minimal = if ($main) { "$($f.name) $token · $main" } else { "$($f.name) $token" }
    if ($minimal.Length -gt $LivenessMaxMinimalRowLength) {
        throw "liveness: minimal row for '$($f.name)' is $($minimal.Length) chars (> $LivenessMaxMinimalRowLength); R-12 relies on this bound"
    }
    return [PSCustomObject]@{
        name     = $f.name
        state    = $st.state
        tier     = $t.tier
        ratio    = $t.ratio
        open     = $openN
        signals  = @($t.signals | ForEach-Object { $_.name })
        full     = (& $render 0)
        level1   = (& $render 1)
        level2   = (& $render 2)
        minimal  = $minimal
        facts    = $f
    }
}

# The single comparator (R-7): tier desc, overdue_ratio desc, open desc, name asc. Display order,
# omission (tail only) and the quiet fold all read this one order.
function Sort-ProjectLivenessRows {
    param([object[]]$Rows)
    return , @(@($Rows) | Sort-Object -Property @{ Expression = { $_.tier }; Descending = $true },
        @{ Expression = { $_.ratio }; Descending = $true },
        @{ Expression = { $_.open }; Descending = $true },
        @{ Expression = { $_.name }; Descending = $false })
}

function Format-LivenessStamp {
    param([datetime]$GeneratedAt)
    return $GeneratedAt.ToUniversalTime().ToString('HH:mm', [Globalization.CultureInfo]::InvariantCulture) + 'Z'
}

# L0 and L1 (msg-2203 §7). Returns @{ L0; L1 (string[]); Omitted (rows, attention order);
# Rows (all rows, attention order) }.
#   * L0 is the unconditional line: tier tally, state tally, open, PR (<= 140).
#   * L1 is the section: header (D-7 note), one line per non-quiet project in attention order,
#     the quiet fold (quiet <=> tier 0), and — only if rows were cut — the omitted line with the
#     tier breakdown and the follow-up pointer (R-12: never a file path). <= 400.
#   * Field ladder first (all rows at the same level, msg-2203 §7 deletion order), then rows from
#     the tail only.
function Get-ProjectLivenessDigestLines {
    param(
        [object[]]$Facts = @(),
        [datetime]$GeneratedAt,
        [string[]]$Unmeasured = @(),
        [int]$L1Budget = $LivenessL1Budget
    )
    $rows = Sort-ProjectLivenessRows -Rows @(@($Facts) | ForEach-Object { Get-ProjectLivenessRow -Facts $_ })
    $tierCount = @{ 3 = 0; 2 = 0; 1 = 0; 0 = 0 }
    $stateCount = @{}
    $openTotal = 0; $prTotal = 0
    foreach ($r in $rows) {
        $tierCount[[int]$r.tier]++
        $stateCount[$r.state] = 1 + [int]$stateCount[$r.state]
        $openTotal += [int]$r.open
        $prTotal += @($r.facts.prs).Count
    }
    $states = @($LivenessStateOrder | Where-Object { [int]$stateCount[$_] -gt 0 } | ForEach-Object { "$($LivenessStateAbbrev[$_])$($stateCount[$_])" }) -join '/'
    if (-not $states) { $states = '-' }
    $l0 = "$(@($rows).Count) projects — attention: $($tierCount[3]) critical / $($tierCount[2]) watch / $($tierCount[1]) hygiene · states $states · open $openTotal · PR $prTotal"
    if (@($Unmeasured).Count -gt 0) { $l0 += " · unmeasured $(@($Unmeasured).Count)" }
    if ($l0.Length -gt $LivenessL0Budget) {
        throw "liveness: L0 is $($l0.Length) chars (> $LivenessL0Budget, msg-2203 §7)"
    }

    $header = 'プロジェクト liveness（日数は last_activity_at の投稿者申告値に依存）'
    $shown = @($rows | Where-Object { [int]$_.tier -gt 0 })
    $quiet = @($rows | Where-Object { [int]$_.tier -eq 0 })
    $quietLine = if ($quiet.Count -gt 0) { "quiet: $($quiet.Count) projects — EMPTY/DONE, no defects" } else { $null }
    $stamp = Format-LivenessStamp -GeneratedAt $GeneratedAt

    $assemble = {
        param([string[]]$Body, [object[]]$Cut)
        $lines = @($header) + @($Body)
        if ($quietLine) { $lines += $quietLine }
        if (@($Cut).Count -gt 0) {
            $parts = @()
            foreach ($tv in 3, 2, 1) {
                $n = @($Cut | Where-Object { [int]$_.tier -eq $tv }).Count
                if ($n -gt 0) { $parts += "$n $($LivenessTierName[$tv])" }
            }
            $lines += "+$(@($Cut).Count) rows omitted ($($parts -join ', ')) → 続報 @$stamp"
        }
        return , $lines
    }
    $cost = { param([string[]]$L) $c = 0; foreach ($x in $L) { $c += $x.Length + 1 }; return $c }

    foreach ($prop in 'full', 'level1', 'level2', 'minimal') {
        $body = @($shown | ForEach-Object { $_.$prop })
        $lines = & $assemble $body @()
        if ((& $cost $lines) -le $L1Budget) {
            return @{ L0 = $l0; L1 = $lines; Omitted = @(); Rows = $rows }
        }
    }
    # Minimal form still overflows: cut rows from the tail only (INV-VIS, msg-2199).
    $keep = $shown.Count
    while ($keep -gt 0) {
        $keep--
        $kept = if ($keep -gt 0) { @($shown[0..($keep - 1)]) } else { @() }
        $cut = @($shown[$keep..($shown.Count - 1)])
        $lines = & $assemble @($kept | ForEach-Object { $_.minimal }) $cut
        if ((& $cost $lines) -le $L1Budget) {
            return @{ L0 = $l0; L1 = $lines; Omitted = $cut; Rows = $rows }
        }
    }
    $lines = & $assemble @() $shown
    return @{ L0 = $l0; L1 = $lines; Omitted = $shown; Rows = $rows }
}

# R-12 (msg-8386): every omitted row is delivered, in attention order, split on row boundaries
# into follow-ups of at most -Budget chars each, each headed `続報 @HH:mmZ (k/n)`. No file path
# appears anywhere in this text (the reader is on Discord and cannot open the execution host's
# disk). Returns string[] — one element per follow-up; empty when nothing was omitted.
function Get-ProjectLivenessContinuationChunks {
    param([object[]]$Omitted = @(), [datetime]$GeneratedAt, [int]$Budget = 1950)
    $rows = @($Omitted)
    if ($rows.Count -eq 0) { return @() }
    $stamp = Format-LivenessStamp -GeneratedAt $GeneratedAt
    # The header's worst case is "(nnn/nnn)"; reserve it so the k/n numbering cannot push a chunk over.
    $headerReserve = ("続報 @$stamp (999/999)").Length + 1
    $groups = @()
    $cur = [System.Collections.Generic.List[string]]::new(); $curCost = $headerReserve
    foreach ($r in $rows) {
        $line = "$($r.minimal)"
        if ($line.Length -gt $LivenessMaxMinimalRowLength) { throw "liveness: follow-up row exceeds $LivenessMaxMinimalRowLength chars" }
        if ($cur.Count -gt 0 -and ($curCost + $line.Length + 1) -gt $Budget) {
            $groups += , @($cur.ToArray()); $cur = [System.Collections.Generic.List[string]]::new(); $curCost = $headerReserve
        }
        $cur.Add($line); $curCost += $line.Length + 1
    }
    if ($cur.Count -gt 0) { $groups += , @($cur.ToArray()) }
    $n = $groups.Count
    $out = @()
    for ($k = 0; $k -lt $n; $k++) {
        $out += ((@("続報 @$stamp ($($k + 1)/$n)") + @($groups[$k])) -join "`n")
    }
    return , $out
}

# R-10 snapshot: generated_at, every row in full form with no omission and no fold, and per-row
# tier / overdue_ratio / state / signals. Pure: the caller writes it with Save-JsonState. It is the
# future L2's content only; nothing in Discord text points at it (R-12).
function ConvertTo-ProjectLivenessSnapshot {
    param([object[]]$Rows = @(), [datetime]$GeneratedAt)
    $list = @()
    foreach ($r in @($Rows)) {
        $list += [ordered]@{
            name = $r.name; state = $r.state; tier = [int]$r.tier
            overdue_ratio = [math]::Round([double]$r.ratio, 3)
            signals = @($r.signals); line = $r.full
        }
    }
    return [ordered]@{
        generated_at = $GeneratedAt.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ', [Globalization.CultureInfo]::InvariantCulture)
        rows = $list
    }
}
