param(
    [Parameter(Mandatory = $true)]
    [string]$EvidenceDirectory,
    [switch]$Interactive,
    [ValidateSet("Validate", "Start", "Status", "Credential", "Stop")]
    [string]$InteractiveAction = "Start",
    [ValidateSet("Local", "OidcTenantA")]
    [string]$Credential = "Local",
    [string]$SessionDirectory,
    [switch]$OwnershipSelfTest,
    [switch]$ObservabilitySelfTest,
    [switch]$ShutdownSelfTest
)

$ErrorActionPreference = "Stop"
if ($null -eq ("System.Security.Cryptography.ProtectedData" -as [type])) {
    Add-Type -AssemblyName System.Security -ErrorAction Stop
}
if ($null -eq ("System.Security.Cryptography.ProtectedData" -as [type])) {
    throw "System.Security DPAPI types are unavailable"
}
if ($null -eq (Get-Command Get-FileHash -ErrorAction SilentlyContinue)) {
    $utilityModule = Join-Path $PSHOME `
        "Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1"
    Import-Module $utilityModule -ErrorAction Stop
}
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$python = Join-Path $repo ".venv\Scripts\python.exe"
$keycloakName = "trackflow-sec114-qa-keycloak-2682"
$postgresName = "trackflow-sec114-qa-postgres-16"
$backofficePort = 28080
$qaPorts = 28082, 55441, 28083, 28084
$tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$defaultInteractiveState = Join-Path $tempRoot "trackflow-sec114-interactive"
$stateDirectory = if ($Interactive) {
    if ($SessionDirectory) { [IO.Path]::GetFullPath($SessionDirectory) } else { $defaultInteractiveState }
} else {
    Join-Path $tempRoot ("trackflow-sec114-qa-" + [guid]::NewGuid())
}
$apiProcess = $null
$mcpProcess = $null
$backofficeProcess = $null
$containersStarted = $false
$preserveInteractive = $false
$sessionManifestPath = Join-Path $stateDirectory "session.json"
$credentialBlobPath = Join-Path $stateDirectory "credentials.dpapi"

function New-EphemeralSecret {
    [byte[]]$bytes = New-Object byte[] 32
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
        return [Convert]::ToBase64String($bytes).Replace("+", "A").Replace("/", "B")
    } finally {
        if ($null -ne $generator) { $generator.Dispose() }
        [Array]::Clear($bytes, 0, $bytes.Length)
    }
}

function Wait-Http([string]$Url) {
    for ($attempt = 0; $attempt -lt 180; $attempt++) {
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -lt 500) { return }
        } catch {
            Start-Sleep -Seconds 1
        }
    }
    throw "Timed out waiting for QA endpoint: $Url"
}

function Assert-InteractiveStatePath {
    $fullState = [IO.Path]::GetFullPath($stateDirectory)
    $tempBoundary = $tempRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) +
        [IO.Path]::DirectorySeparatorChar
    if (-not $fullState.StartsWith($tempBoundary, [StringComparison]::OrdinalIgnoreCase) -or
        $fullState -eq $tempRoot) {
        throw "Interactive SEC-114 state must be a dedicated directory under the OS temp directory"
    }
}

function Assert-InteractiveEvidencePath {
    $fullState = [IO.Path]::GetFullPath($stateDirectory).TrimEnd(
        [IO.Path]::DirectorySeparatorChar
    )
    $stateBoundary = $fullState + [IO.Path]::DirectorySeparatorChar
    $fullEvidence = [IO.Path]::GetFullPath($EvidenceDirectory)
    if ($fullEvidence -eq $fullState -or
        $fullEvidence.StartsWith($stateBoundary, [StringComparison]::OrdinalIgnoreCase)) {
        throw "SEC-114 evidence must be outside the disposable interactive state directory"
    }
}

function Assert-Prerequisites {
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "Repository Python virtual environment is missing: $python"
    }
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "Docker CLI is required for SEC-114 QA"
    }
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw "uv is required for the MCP runtime"
    }
    if ($Interactive -and -not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) {
        throw "npm is required for the Backoffice"
    }
    if ($Interactive -and
        -not (Test-Path -LiteralPath (Join-Path $repo "uis\backoffice\node_modules\.bin\next.cmd"))) {
        throw "Backoffice dependencies are missing; run npm ci in uis/backoffice before starting acceptance"
    }
    if ($Interactive) {
        $randomProbe = New-EphemeralSecret
        if ([string]::IsNullOrWhiteSpace($randomProbe) -or $randomProbe.Length -lt 32) {
            throw "Cryptographic random generation is unavailable for SEC-114 credentials"
        }
        $randomProbe = $null
        $probe = [byte[]](17, 34, 51)
        $protectedProbe = [Security.Cryptography.ProtectedData]::Protect(
            $probe,
            $null,
            [Security.Cryptography.DataProtectionScope]::CurrentUser
        )
        $roundTrip = [Security.Cryptography.ProtectedData]::Unprotect(
            $protectedProbe,
            $null,
            [Security.Cryptography.DataProtectionScope]::CurrentUser
        )
        if ($roundTrip.Length -ne $probe.Length -or
            (Compare-Object $probe $roundTrip)) {
            throw "Windows DPAPI is unavailable for SEC-114 credential protection"
        }
        [Array]::Clear($roundTrip, 0, $roundTrip.Length)
        [Array]::Clear($protectedProbe, 0, $protectedProbe.Length)
        [Array]::Clear($probe, 0, $probe.Length)
        Test-ProcessOwnershipLogic
    }
}

function Assert-NoCollisions {
    $ports = if ($Interactive) { @($qaPorts) + $backofficePort } else { $qaPorts }
    $collisions = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalPort -in $ports }
    if ($collisions) {
        $used = ($collisions.LocalPort | Sort-Object -Unique) -join ", "
        throw "SEC-114 QA port collision detected: $used"
    }
    $existing = docker ps -a --filter "name=trackflow-sec114-qa" --format "{{.Names}}"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not inspect Docker for SEC-114 container-name collisions"
    }
    if ($existing) { throw "SEC-114 QA container-name collision detected" }
}

function Protect-InteractiveCredentials([hashtable]$Values) {
    $plain = [Text.Encoding]::UTF8.GetBytes(($Values | ConvertTo-Json -Compress))
    $protected = $null
    try {
        $protected = [Security.Cryptography.ProtectedData]::Protect(
            $plain,
            $null,
            [Security.Cryptography.DataProtectionScope]::CurrentUser
        )
        [Convert]::ToBase64String($protected) |
            Set-Content -LiteralPath $credentialBlobPath -Encoding ASCII -NoNewline
    } finally {
        if ($null -ne $protected) {
            [Array]::Clear($protected, 0, $protected.Length)
        }
        [Array]::Clear($plain, 0, $plain.Length)
    }
}

function Unprotect-InteractiveCredentials {
    $protected = [Convert]::FromBase64String(
        (Get-Content -Raw -LiteralPath $credentialBlobPath)
    )
    $plain = [Security.Cryptography.ProtectedData]::Unprotect(
        $protected,
        $null,
        [Security.Cryptography.DataProtectionScope]::CurrentUser
    )
    try {
        return ([Text.Encoding]::UTF8.GetString($plain) | ConvertFrom-Json)
    } finally {
        [Array]::Clear($plain, 0, $plain.Length)
        [Array]::Clear($protected, 0, $protected.Length)
    }
}

function Read-InteractiveManifest {
    Assert-InteractiveStatePath
    if (-not (Test-Path -LiteralPath $sessionManifestPath -PathType Leaf)) {
        throw "No active SEC-114 interactive session was found"
    }
    $manifest = Get-Content -Raw -LiteralPath $sessionManifestPath | ConvertFrom-Json
    if ($manifest.schema_version -ne 1 -or $manifest.mode -ne "sec114-interactive" -or
        $manifest.repository -ne $repo) {
        throw "SEC-114 interactive session manifest is invalid"
    }
    return $manifest
}

function Get-GuardrailCounts([string]$ApiLogPath) {
    $counts = @{}
    if (-not (Test-Path -LiteralPath $ApiLogPath -PathType Leaf)) { return $counts }
    foreach ($line in Get-Content -LiteralPath $ApiLogPath) {
        if ($line -notmatch 'TrackFlow guardrail event \| (\{.*\})') { continue }
        try {
            $event = $Matches[1] | ConvertFrom-Json
            $guardId = [string]$event.guard_id
            $failureCategory = [string]$event.failure_category
            $action = [string]$event.action
            $reasonCode = [string]$event.reason_code
            if ($guardId -notmatch '^[a-z0-9._-]{1,64}$' -or
                $failureCategory -notin @('structural', 'content', 'security') -or
                $action -notin @('blocked', 'redirected') -or
                $reasonCode -notmatch '^[a-z][a-z0-9_]{0,63}$') {
                throw "invalid event dimensions"
            }
            $key = "$guardId|$failureCategory|$action|$reasonCode"
            if (-not $counts.ContainsKey($key)) { $counts[$key] = 0 }
            $counts[$key] += 1
        } catch {
            throw "Could not parse sanitized SEC-114 guardrail event"
        }
    }
    return $counts
}

function Get-LogMatchCount([string]$Path, [string]$Pattern) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return 0 }
    return @(
        Select-String -LiteralPath $Path -Pattern $Pattern -AllMatches
    ).Count
}

function Get-McpStdoutPath($Manifest) {
    if ($null -ne $Manifest.logs.mcp_stdout -and
        -not [string]::IsNullOrWhiteSpace([string]$Manifest.logs.mcp_stdout)) {
        return [string]$Manifest.logs.mcp_stdout
    }
    return Join-Path (Split-Path -Parent ([string]$Manifest.logs.mcp_stderr)) `
        "mcp.stdout.log"
}

function Get-McpBusinessToolCounts([string]$Path) {
    $counts = @{}
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $counts }
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -notmatch 'TrackFlow MCP tool event \| (\{.*\})') { continue }
        try {
            $event = $Matches[1] | ConvertFrom-Json
            $tool = [string]$event.tool
            $result = [string]$event.result
            if ($tool -notmatch '^[a-z][a-z0-9_]{0,63}$' -or
                $result -notmatch '^[a-z][a-z0-9_]{0,31}$') {
                throw "invalid event dimensions"
            }
            if (-not $counts.ContainsKey($tool)) { $counts[$tool] = @{} }
            if (-not $counts[$tool].ContainsKey($result)) { $counts[$tool][$result] = 0 }
            $counts[$tool][$result] += 1
        } catch {
            throw "Could not parse sanitized SEC-114 MCP tool event"
        }
    }
    return $counts
}

function Get-SanitizedObservability($Manifest) {
    $mcpStdout = Get-McpStdoutPath $Manifest
    $mcpStderr = [string]$Manifest.logs.mcp_stderr
    $transportPosts = Get-LogMatchCount $mcpStdout 'POST /mcp'
    return [ordered]@{
        guardrail_counts = Get-GuardrailCounts $Manifest.logs.api_stderr
        sanitized_http_counts = [ordered]@{
            support_query = Get-LogMatchCount $Manifest.logs.api_stderr 'POST /support/query'
            # Backward-compatible alias. This is a transport count, never a
            # business-tool invocation count.
            mcp_post = $transportPosts
            mcp_transport_post = $transportPosts
        }
        mcp_business_tool_counts = Get-McpBusinessToolCounts $mcpStderr
        mcp_legacy_unclassified_tool_events = `
            Get-LogMatchCount $mcpStderr 'trackflow_mcp_tool'
    }
}

function Test-ObservabilityCountingLogic {
    $fixtureRoot = Join-Path $tempRoot ("trackflow-sec114-observability-test-" + [guid]::NewGuid())
    New-Item -ItemType Directory -Path $fixtureRoot | Out-Null
    try {
        $apiLog = Join-Path $fixtureRoot "api.stderr.log"
        $mcpOut = Join-Path $fixtureRoot "mcp.stdout.log"
        $mcpErr = Join-Path $fixtureRoot "mcp.stderr.log"
        @(
            'INFO TrackFlow guardrail event | {"action":"blocked","failure_category":"security","guard_id":"support.input.v1","reason_code":"instruction_override"}',
            'INFO TrackFlow guardrail event | {"action":"redirected","failure_category":"content","guard_id":"support.input.v1","reason_code":"smalltalk"}',
            'INFO TrackFlow guardrail event | {"action":"redirected","failure_category":"structural","guard_id":"support.input.v1","reason_code":"missing_identifier"}',
            'INFO "POST /support/query HTTP/1.1" 200'
        ) | Set-Content -LiteralPath $apiLog -Encoding UTF8
        @(
            'INFO "POST /mcp HTTP/1.1" 202',
            'INFO "POST /mcp HTTP/1.1" 200'
        ) | Set-Content -LiteralPath $mcpOut -Encoding UTF8
        @(
            'INFO TrackFlow MCP tool event | {"downstream_status":200,"duration_ms":1.25,"result":"success","tool":"get_inventory_product"}',
            'INFO trackflow_mcp_tool'
        ) | Set-Content -LiteralPath $mcpErr -Encoding UTF8
        $manifest = [pscustomobject]@{
            logs = [pscustomobject]@{
                api_stderr = $apiLog
                mcp_stdout = $mcpOut
                mcp_stderr = $mcpErr
            }
        }
        $observed = Get-SanitizedObservability $manifest
        if ($observed.sanitized_http_counts.support_query -ne 1 -or
            $observed.sanitized_http_counts.mcp_transport_post -ne 2 -or
            $observed.mcp_business_tool_counts.get_inventory_product.success -ne 1 -or
            $observed.mcp_legacy_unclassified_tool_events -ne 1 -or
            $observed.guardrail_counts.'support.input.v1|security|blocked|instruction_override' -ne 1 -or
            $observed.guardrail_counts.'support.input.v1|content|redirected|smalltalk' -ne 1 -or
            $observed.guardrail_counts.'support.input.v1|structural|redirected|missing_identifier' -ne 1) {
            throw "SEC-114 observability counter self-test failed"
        }
        $missing = Join-Path $fixtureRoot "missing.log"
        if ((Get-LogMatchCount $missing 'POST /mcp') -ne 0 -or
            (Get-McpBusinessToolCounts $missing).Count -ne 0 -or
            (Get-GuardrailCounts $missing).Count -ne 0) {
            throw "SEC-114 missing-log observability self-test failed"
        }
        $invalidLog = Join-Path $fixtureRoot "invalid-guardrail.log"
        'INFO TrackFlow guardrail event | {"action":"blocked","failure_category":"unknown","guard_id":"support.input.v1","reason_code":"instruction_override"}' |
            Set-Content -LiteralPath $invalidLog -Encoding UTF8
        $invalidRejected = $false
        try {
            Get-GuardrailCounts $invalidLog | Out-Null
        } catch {
            $invalidRejected = $true
        }
        if (-not $invalidRejected) {
            throw "SEC-114 invalid guardrail category self-test failed"
        }
    } finally {
        if (Test-Path -LiteralPath $fixtureRoot) {
            Remove-Item -LiteralPath $fixtureRoot -Recurse -Force
        }
    }
}

function Test-CommandMarkers([string]$CommandLine, [string[]]$Markers) {
    if ([string]::IsNullOrWhiteSpace($CommandLine)) { return $false }
    foreach ($marker in $Markers) {
        if ($CommandLine.IndexOf($marker, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
            return $false
        }
    }
    return $true
}

function Convert-ProcessCreationToUtc($Value) {
    if ($Value -is [DateTime]) { return ([DateTime]$Value).ToUniversalTime() }
    return ([Management.ManagementDateTimeConverter]::ToDateTime([string]$Value)).ToUniversalTime()
}

function Get-ManifestSessionUtc($Manifest) {
    $value = $Manifest.created_at_utc
    if ($value -is [DateTime]) { return ([DateTime]$value).ToUniversalTime() }
    return [DateTimeOffset]::Parse([string]$value).UtcDateTime
}

function Resolve-OwnedProcessChain(
    [int]$ListenerProcessId,
    [int]$ExpectedLauncherPid,
    [DateTime]$SessionCreatedUtc,
    [string[]]$LauncherMarkers,
    [string]$ServiceMarker,
    [hashtable]$ProcessTable = $null,
    $ExpectedLauncherCreatedUtc = $null
) {
    if ($ListenerProcessId -le 0 -or $ExpectedLauncherPid -le 0) {
        throw "SEC-114 process ownership identifiers are invalid"
    }
    $chain = @()
    $seen = @{}
    $cursorProcessId = $ListenerProcessId
    $serviceMarkerFound = $false
    for ($depth = 0; $depth -lt 32; $depth++) {
        if ($seen.ContainsKey($cursorProcessId)) {
            throw "SEC-114 process ownership chain contains a cycle"
        }
        $seen[$cursorProcessId] = $true
        if ($null -ne $ProcessTable) {
            $process = $ProcessTable[$cursorProcessId]
        } else {
            $process = Get-CimInstance Win32_Process `
                -Filter "ProcessId=$cursorProcessId" -ErrorAction SilentlyContinue
        }
        if ($null -eq $process) {
            throw "SEC-114 process ownership chain is incomplete"
        }
        $createdUtc = Convert-ProcessCreationToUtc $process.CreationDate
        if ($createdUtc -lt $SessionCreatedUtc.AddMinutes(-5) -or
            $createdUtc -gt [DateTime]::UtcNow.AddMinutes(1)) {
            throw "SEC-114 process creation time is outside the session window"
        }
        $commandLine = [string]$process.CommandLine
        if (Test-CommandMarkers $commandLine @($ServiceMarker)) {
            $serviceMarkerFound = $true
        }
        $chain += [pscustomobject]@{
            Pid = [int]$process.ProcessId
            ParentPid = [int]$process.ParentProcessId
            CreatedUtc = $createdUtc
            Depth = $depth
            CommandLine = $commandLine
        }
        if ([int]$process.ProcessId -eq $ExpectedLauncherPid) { break }
        $cursorProcessId = [int]$process.ParentProcessId
        if ($cursorProcessId -le 0) {
            throw "SEC-114 listener is not a descendant of the recorded launcher"
        }
    }
    if ($chain.Count -eq 0 -or $chain[-1].Pid -ne $ExpectedLauncherPid) {
        throw "SEC-114 listener is not a descendant of the recorded launcher"
    }
    if (-not (Test-CommandMarkers $chain[-1].CommandLine $LauncherMarkers)) {
        throw "SEC-114 recorded launcher identity does not match the session"
    }
    if ($null -ne $ExpectedLauncherCreatedUtc -and
        [Math]::Abs(($chain[-1].CreatedUtc - $ExpectedLauncherCreatedUtc).TotalSeconds) -gt 2) {
        throw "SEC-114 recorded launcher creation time does not match the session"
    }
    if (-not $serviceMarkerFound) {
        throw "SEC-114 service marker is absent from the owned process chain"
    }
    for ($index = 0; $index -lt ($chain.Count - 1); $index++) {
        $child = $chain[$index]
        $parent = $chain[$index + 1]
        if ($child.ParentPid -ne $parent.Pid -or
            $child.CreatedUtc -lt $parent.CreatedUtc.AddSeconds(-2)) {
            throw "SEC-114 process hierarchy or creation order is invalid"
        }
    }
    return $chain
}

function Get-InteractiveServiceSpecs($Manifest) {
    $backofficeCreated = $Manifest.processes.backoffice.created_utc
    $mcpCreated = $Manifest.processes.mcp.created_utc
    $apiCreated = $Manifest.processes.api.created_utc
    return @(
        [pscustomobject]@{
            Name = "backoffice"; Port = $backofficePort
            Pid = [int]$Manifest.processes.backoffice.pid
            LauncherMarkers = @("npm", "run", "dev", "$backofficePort")
            ServiceMarker = "next"
            ExpectedCreatedUtc = if ($null -ne $backofficeCreated) {
                ([DateTimeOffset]::Parse([string]$backofficeCreated)).UtcDateTime
            } else { $null }
        },
        [pscustomobject]@{
            Name = "mcp"; Port = 28084; Pid = [int]$Manifest.processes.mcp.pid
            LauncherMarkers = @("trackflow_mcp.runtime:create_runtime_app", "28084")
            ServiceMarker = "trackflow_mcp.runtime:create_runtime_app"
            ExpectedCreatedUtc = if ($null -ne $mcpCreated) {
                ([DateTimeOffset]::Parse([string]$mcpCreated)).UtcDateTime
            } else { $null }
        },
        [pscustomobject]@{
            Name = "api"; Port = 28083; Pid = [int]$Manifest.processes.api.pid
            LauncherMarkers = @("services.api.main:app", "28083")
            ServiceMarker = "services.api.main:app"
            ExpectedCreatedUtc = if ($null -ne $apiCreated) {
                ([DateTimeOffset]::Parse([string]$apiCreated)).UtcDateTime
            } else { $null }
        }
    )
}

function Get-OwnedListenerChain($Service, $Manifest) {
    $listeners = @(
        Get-NetTCPConnection -State Listen -LocalPort $Service.Port `
            -ErrorAction SilentlyContinue |
            Where-Object { $_.LocalAddress -eq "127.0.0.1" }
    )
    if ($listeners.Count -ne 1) {
        throw "SEC-114 service does not have exactly one owned loopback listener on port $($Service.Port)"
    }
    return @(
        Resolve-OwnedProcessChain `
            -ListenerProcessId ([int]$listeners[0].OwningProcess) `
            -ExpectedLauncherPid ([int]$Service.Pid) `
            -SessionCreatedUtc (Get-ManifestSessionUtc $Manifest) `
            -LauncherMarkers $Service.LauncherMarkers `
            -ServiceMarker $Service.ServiceMarker `
            -ExpectedLauncherCreatedUtc $Service.ExpectedCreatedUtc
    )
}

function Test-OwnedListener($Service, $Manifest) {
    $chain = @(Get-OwnedListenerChain $Service $Manifest)
    return $chain.Count -gt 0
}

function Get-ProcessForShutdown([int]$ProcessId, [hashtable]$ProcessTable = $null) {
    if ($null -ne $ProcessTable) { return $ProcessTable[$ProcessId] }
    return Get-CimInstance Win32_Process `
        -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
}

function Get-OwnedProcessTree(
    [int]$RootPid,
    [DateTime]$RootCreatedUtc,
    [hashtable]$ProcessTable = $null
) {
    $root = Get-ProcessForShutdown $RootPid $ProcessTable
    if ($null -eq $root) { return @() }
    $actualRootCreatedUtc = Convert-ProcessCreationToUtc $root.CreationDate
    if ([Math]::Abs(($actualRootCreatedUtc - $RootCreatedUtc).TotalSeconds) -gt 2) {
        throw "SEC-114 recorded launcher PID was reused"
    }
    $all = if ($null -ne $ProcessTable) {
        @($ProcessTable.Values)
    } else {
        @(Get-CimInstance Win32_Process)
    }
    $tree = @([pscustomobject]@{
        Pid = $RootPid
        ParentPid = [int]$root.ParentProcessId
        CreatedUtc = $actualRootCreatedUtc
        Depth = 0
    })
    $frontier = @($tree[0])
    while ($frontier.Count -gt 0) {
        $next = @()
        foreach ($parent in $frontier) {
            foreach ($child in @($all | Where-Object { [int]$_.ParentProcessId -eq $parent.Pid })) {
                $createdUtc = Convert-ProcessCreationToUtc $child.CreationDate
                if ($createdUtc -lt $parent.CreatedUtc.AddSeconds(-2)) {
                    throw "SEC-114 descendant creation order is invalid"
                }
                $record = [pscustomobject]@{
                    Pid = [int]$child.ProcessId
                    ParentPid = [int]$child.ParentProcessId
                    CreatedUtc = $createdUtc
                    Depth = $parent.Depth + 1
                }
                $tree += $record
                $next += $record
            }
        }
        $frontier = $next
    }
    return $tree
}

function Get-RecoverableServiceTree(
    $Service,
    [DateTime]$SessionCreatedUtc,
    [hashtable]$ProcessTable = $null
) {
    $launcher = Get-ProcessForShutdown ([int]$Service.Pid) $ProcessTable
    if ($null -eq $launcher) { return @() }
    $actualCreatedUtc = Convert-ProcessCreationToUtc $launcher.CreationDate
    if ($actualCreatedUtc -lt $SessionCreatedUtc.AddMinutes(-5) -or
        $actualCreatedUtc -gt [DateTime]::UtcNow.AddMinutes(1)) {
        throw "SEC-114 recorded launcher creation time is outside the session window"
    }
    if ($null -ne $Service.ExpectedCreatedUtc -and
        [Math]::Abs(($actualCreatedUtc - $Service.ExpectedCreatedUtc).TotalSeconds) -gt 2) {
        throw "SEC-114 recorded launcher PID was reused"
    }
    if (-not (Test-CommandMarkers ([string]$launcher.CommandLine) $Service.LauncherMarkers)) {
        throw "SEC-114 recorded launcher identity does not match the session"
    }
    return @(
        Get-OwnedProcessTree `
            -RootPid ([int]$Service.Pid) `
            -RootCreatedUtc $actualCreatedUtc `
            -ProcessTable $ProcessTable
    )
}

function Get-ServiceShutdownTree($Service, $Manifest) {
    $listeners = @(
        Get-NetTCPConnection -State Listen -LocalPort $Service.Port `
            -ErrorAction SilentlyContinue
    )
    if ($listeners.Count -eq 0) {
        return @(
            Get-RecoverableServiceTree `
                -Service $Service `
                -SessionCreatedUtc (Get-ManifestSessionUtc $Manifest)
        )
    }
    if ($listeners.Count -ne 1 -or $listeners[0].LocalAddress -ne "127.0.0.1") {
        throw "SEC-114 shutdown found an unexpected listener on port $($Service.Port)"
    }
    $chain = @(
        Resolve-OwnedProcessChain `
            -ListenerProcessId ([int]$listeners[0].OwningProcess) `
            -ExpectedLauncherPid ([int]$Service.Pid) `
            -SessionCreatedUtc (Get-ManifestSessionUtc $Manifest) `
            -LauncherMarkers $Service.LauncherMarkers `
            -ServiceMarker $Service.ServiceMarker `
            -ExpectedLauncherCreatedUtc $Service.ExpectedCreatedUtc
    )
    return @(
        Get-OwnedProcessTree `
            -RootPid ([int]$Service.Pid) `
            -RootCreatedUtc $chain[-1].CreatedUtc
    )
}

function Stop-ValidatedProcessTree(
    $Records,
    [hashtable]$ProcessTable = $null,
    [scriptblock]$StopAction = $null
) {
    $seen = @{}
    foreach ($owned in @($Records | Sort-Object Depth -Descending)) {
        $processId = [int]$owned.Pid
        if ($seen.ContainsKey($processId)) { continue }
        $seen[$processId] = $true
        $running = Get-ProcessForShutdown $processId $ProcessTable
        if ($null -eq $running) { continue }
        $actualCreatedUtc = Convert-ProcessCreationToUtc $running.CreationDate
        if ([Math]::Abs(($actualCreatedUtc - $owned.CreatedUtc).TotalSeconds) -gt 2) {
            throw "SEC-114 process PID was reused before shutdown"
        }
        try {
            if ($null -ne $StopAction) {
                & $StopAction $processId $ProcessTable
            } else {
                Stop-Process -Id $processId -Force -ErrorAction Stop
            }
        } catch {
            $afterFailure = Get-ProcessForShutdown $processId $ProcessTable
            if ($null -eq $afterFailure) { continue }
            $afterCreatedUtc = Convert-ProcessCreationToUtc $afterFailure.CreationDate
            if ([Math]::Abs(($afterCreatedUtc - $owned.CreatedUtc).TotalSeconds) -gt 2) {
                throw "SEC-114 process PID was reused during shutdown"
            }
            throw
        }
    }
}

function Assert-ContainerIdentity($Record, [string]$ExpectedName, $ActualId = $null) {
    if ([string]$Record.name -ne $ExpectedName -or
        [string]::IsNullOrWhiteSpace([string]$Record.id)) {
        throw "SEC-114 container manifest identity is invalid"
    }
    if ($null -ne $ActualId -and [string]$ActualId -ne [string]$Record.id) {
        throw "SEC-114 container ownership mismatch: $ExpectedName"
    }
}

function Get-ValidatedContainerState($Record, [string]$ExpectedName) {
    Assert-ContainerIdentity $Record $ExpectedName
    $named = @(
        docker ps -a --filter "name=$ExpectedName" --format "{{.Names}}" 2>$null |
            Where-Object { $_ -eq $ExpectedName }
    )
    if ($LASTEXITCODE -ne 0) {
        throw "Could not inspect Docker for SEC-114 container ownership"
    }
    if ($named.Count -eq 0) {
        return [pscustomobject]@{ Name=$ExpectedName; Exists=$false; Running=$false }
    }
    if ($named.Count -ne 1) {
        throw "SEC-114 container name is ambiguous: $ExpectedName"
    }
    $actual = docker inspect --format "{{.Id}}|{{.State.Running}}" $ExpectedName 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not inspect SEC-114 container identity: $ExpectedName"
    }
    $values = @($actual -split '\|')
    if ($values.Count -ne 2) {
        throw "SEC-114 container inspection returned an invalid response"
    }
    try {
        Assert-ContainerIdentity $Record $ExpectedName $values[0]
    } catch {
        throw "SEC-114 container ownership mismatch: $ExpectedName"
    }
    return [pscustomobject]@{
        Name = $ExpectedName
        Exists = $true
        Running = $values[1] -eq "true"
    }
}

function Confirm-InteractiveAlreadyStopped {
    if (Test-Path -LiteralPath $stateDirectory) {
        throw "SEC-114 interactive state is incomplete; manual inspection is required"
    }
    $remainingListeners = @(
        Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
            Where-Object { $_.LocalPort -in (@($backofficePort) + @($qaPorts)) }
    )
    if ($remainingListeners.Count -ne 0) {
        throw "SEC-114 has no manifest but a reserved port is still in use"
    }
    foreach ($name in @($keycloakName, $postgresName)) {
        $named = @(
            docker ps -a --filter "name=$name" --format "{{.Names}}" 2>$null |
                Where-Object { $_ -eq $name }
        )
        if ($LASTEXITCODE -ne 0) {
            throw "Could not verify completed SEC-114 container shutdown"
        }
        if ($named.Count -ne 0) {
            throw "SEC-114 has no manifest but a reserved container still exists"
        }
    }
    Write-Output "SEC114_INTERACTIVE_ALREADY_STOPPED"
}

function Test-ProcessOwnershipLogic {
    $now = [DateTime]::UtcNow
    $valid = @{
        100 = [pscustomobject]@{ ProcessId=100; ParentProcessId=1; CreationDate=$now.AddSeconds(-3); CommandLine="npm.cmd run dev -- --port 28080" }
        110 = [pscustomobject]@{ ProcessId=110; ParentProcessId=100; CreationDate=$now.AddSeconds(-2); CommandLine="next dev 28080" }
        120 = [pscustomobject]@{ ProcessId=120; ParentProcessId=110; CreationDate=$now.AddSeconds(-1); CommandLine="next-server uis\backoffice" }
    }
    $chain = @(Resolve-OwnedProcessChain 120 100 $now @("npm","run","dev","28080") "next" $valid)
    if ($chain.Count -ne 3) { throw "SEC-114 valid child ownership self-test failed" }
    $alien = @{
        200 = [pscustomobject]@{ ProcessId=200; ParentProcessId=201; CreationDate=$now.AddSeconds(-1); CommandLine="next-server uis\backoffice" }
        201 = [pscustomobject]@{ ProcessId=201; ParentProcessId=1; CreationDate=$now.AddSeconds(-2); CommandLine="npm.cmd run dev -- --port 28080" }
    }
    $rejected = $false
    try {
        Resolve-OwnedProcessChain 200 100 $now @("npm","run","dev","28080") "next" $alien | Out-Null
    } catch {
        $rejected = $true
    }
    if (-not $rejected) { throw "SEC-114 alien listener ownership self-test failed" }
}

function Test-OwnedListenerIfPresent($Service, $Manifest) {
    $listeners = @(
        Get-NetTCPConnection -State Listen -LocalPort $Service.Port `
            -ErrorAction SilentlyContinue
    )
    if ($listeners.Count -eq 0) { return $false }
    if ($listeners.Count -ne 1 -or $listeners[0].LocalAddress -ne "127.0.0.1") {
        throw "SEC-114 evidence found an unexpected listener on port $($Service.Port)"
    }
    return Test-OwnedListener $Service $Manifest
}

function Write-InteractiveEvidence($Manifest) {
    $evidencePath = [IO.Path]::GetFullPath([string]$Manifest.evidence_directory)
    New-Item -ItemType Directory -Path $evidencePath -Force | Out-Null
    $specs = @(Get-InteractiveServiceSpecs $Manifest)
    $observability = Get-SanitizedObservability $Manifest
    $readiness = [ordered]@{
        backoffice = Test-OwnedListenerIfPresent ($specs | Where-Object Name -eq "backoffice") $Manifest
        api = Test-OwnedListenerIfPresent ($specs | Where-Object Name -eq "api") $Manifest
        mcp = Test-OwnedListenerIfPresent ($specs | Where-Object Name -eq "mcp") $Manifest
        keycloak = [bool](docker ps --filter "id=$($Manifest.containers.keycloak.id)" --format "{{.ID}}")
        postgresql = [bool](docker ps --filter "id=$($Manifest.containers.postgresql.id)" --format "{{.ID}}")
    }
    $summaryPath = Join-Path $evidencePath "sec114-interactive-summary.json"
    [ordered]@{
        gate = "SEC-114-interactive-acceptance"
        captured_at_utc = [DateTime]::UtcNow.ToString("o")
        session_id = $Manifest.session_id
        readiness = $readiness
        guardrail_counts = $observability.guardrail_counts
        sanitized_http_counts = $observability.sanitized_http_counts
        mcp_business_tool_counts = $observability.mcp_business_tool_counts
        mcp_legacy_unclassified_tool_events = `
            $observability.mcp_legacy_unclassified_tool_events
        secrets_recorded = $false
        http_response_bodies_recorded = $false
        note = "Visual responses remain human evidence; this file contains only readiness and content-free guardrail counters."
    } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $summaryPath -Encoding UTF8
    Get-ChildItem -LiteralPath $evidencePath -File |
        Where-Object { $_.Name -ne "sec114-interactive-hashes.csv" } |
        Get-FileHash -Algorithm SHA256 |
        Select-Object Algorithm, Hash, Path |
        Export-Csv -NoTypeInformation -Encoding UTF8 -LiteralPath `
            (Join-Path $evidencePath "sec114-interactive-hashes.csv")
}

function Confirm-OrWriteInteractiveEvidence($Manifest) {
    $evidencePath = [IO.Path]::GetFullPath([string]$Manifest.evidence_directory)
    $summaryPath = Join-Path $evidencePath "sec114-interactive-summary.json"
    $hashesPath = Join-Path $evidencePath "sec114-interactive-hashes.csv"
    $summaryExists = Test-Path -LiteralPath $summaryPath -PathType Leaf
    $hashesExist = Test-Path -LiteralPath $hashesPath -PathType Leaf
    if ($summaryExists -xor $hashesExist) {
        throw "SEC-114 interactive evidence is incomplete; refusing to overwrite it"
    }
    if (-not $summaryExists) {
        Write-InteractiveEvidence $Manifest
        return
    }
    $summary = Get-Content -Raw -LiteralPath $summaryPath | ConvertFrom-Json
    if ($summary.gate -ne "SEC-114-interactive-acceptance" -or
        $summary.secrets_recorded -ne $false -or
        $summary.http_response_bodies_recorded -ne $false) {
        throw "SEC-114 interactive evidence summary is invalid"
    }
    $rows = @(Import-Csv -LiteralPath $hashesPath)
    if ($rows.Count -lt 1) {
        throw "SEC-114 interactive evidence hash manifest is empty"
    }
    $summaryCovered = $false
    foreach ($row in $rows) {
        $target = [IO.Path]::GetFullPath([string]$row.Path)
        if (-not $target.StartsWith(
            $evidencePath + [IO.Path]::DirectorySeparatorChar,
            [StringComparison]::OrdinalIgnoreCase
        )) {
            throw "SEC-114 evidence hash target is outside the evidence directory"
        }
        if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
            throw "SEC-114 evidence hash target is missing"
        }
        $actualHash = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash
        if ($actualHash -ne [string]$row.Hash) {
            throw "SEC-114 interactive evidence integrity check failed"
        }
        if ($target -eq [IO.Path]::GetFullPath($summaryPath)) {
            $summaryCovered = $true
        }
    }
    if (-not $summaryCovered) {
        throw "SEC-114 evidence summary is not covered by the hash manifest"
    }
}

function Test-ShutdownRecoveryLogic {
    $now = [DateTime]::UtcNow
    $service = [pscustomobject]@{
        Pid = 100
        LauncherMarkers = @("launcher", "28080")
        ExpectedCreatedUtc = $now.AddSeconds(-2)
    }
    $alreadyStoppedTree = @(
        Get-RecoverableServiceTree $service $now @{}
    )
    if ($alreadyStoppedTree.Count -ne 0) {
        throw "SEC-114 already-stopped launcher self-test failed"
    }
    $recoverableTable = @{
        100 = [pscustomobject]@{ ProcessId=100; ParentProcessId=1; CreationDate=$now.AddSeconds(-2); CommandLine="launcher --port 28080" }
        110 = [pscustomobject]@{ ProcessId=110; ParentProcessId=100; CreationDate=$now.AddSeconds(-1); CommandLine="child" }
    }
    $recoverableTree = @(
        Get-RecoverableServiceTree $service $now $recoverableTable
    )
    if ($recoverableTree.Count -ne 2) {
        throw "SEC-114 recoverable launcher tree self-test failed"
    }
    $alienLauncher = @{
        100 = [pscustomobject]@{ ProcessId=100; ParentProcessId=1; CreationDate=$now.AddSeconds(-2); CommandLine="alien --port 28080" }
    }
    $alienLauncherRejected = $false
    try {
        Get-RecoverableServiceTree $service $now $alienLauncher | Out-Null
    } catch {
        $alienLauncherRejected = $true
    }
    if (-not $alienLauncherRejected) {
        throw "SEC-114 alien launcher recovery self-test failed"
    }

    $records = @(
        [pscustomobject]@{ Pid=110; ParentPid=100; CreatedUtc=$now.AddSeconds(-1); Depth=1 },
        [pscustomobject]@{ Pid=100; ParentPid=1; CreatedUtc=$now.AddSeconds(-2); Depth=0 }
    )

    # Complete tree: descendants stop before their verified launcher.
    $complete = @{
        100 = [pscustomobject]@{ ProcessId=100; ParentProcessId=1; CreationDate=$now.AddSeconds(-2); CommandLine="launcher" }
        110 = [pscustomobject]@{ ProcessId=110; ParentProcessId=100; CreationDate=$now.AddSeconds(-1); CommandLine="child" }
    }
    $stopped = New-Object Collections.ArrayList
    $stopFake = {
        param($ProcessId, $Table)
        [void]$stopped.Add($ProcessId)
        [void]$Table.Remove($ProcessId)
    }.GetNewClosure()
    Stop-ValidatedProcessTree $records $complete $stopFake
    if (($stopped -join ",") -ne "110,100" -or $complete.Count -ne 0) {
        throw "SEC-114 complete shutdown self-test failed"
    }

    # Partial tree: an already-finished child is success, not an ownership bypass.
    $partial = @{
        100 = [pscustomobject]@{ ProcessId=100; ParentProcessId=1; CreationDate=$now.AddSeconds(-2); CommandLine="launcher" }
    }
    $partialStopped = New-Object Collections.ArrayList
    $stopPartial = {
        param($ProcessId, $Table)
        [void]$partialStopped.Add($ProcessId)
        [void]$Table.Remove($ProcessId)
    }.GetNewClosure()
    Stop-ValidatedProcessTree $records $partial $stopPartial
    if (($partialStopped -join ",") -ne "100") {
        throw "SEC-114 partial shutdown self-test failed"
    }

    # Race: a verified process may disappear between the final read and stop.
    $racing = @{
        100 = [pscustomobject]@{ ProcessId=100; ParentProcessId=1; CreationDate=$now.AddSeconds(-2); CommandLine="launcher" }
    }
    $disappear = {
        param($ProcessId, $Table)
        [void]$Table.Remove($ProcessId)
        throw "simulated process already exited"
    }
    Stop-ValidatedProcessTree @($records | Where-Object Pid -eq 100) $racing $disappear

    # A reused PID and an alien container identity remain fatal.
    $reused = @{
        100 = [pscustomobject]@{ ProcessId=100; ParentProcessId=1; CreationDate=$now.AddMinutes(2); CommandLine="launcher" }
    }
    $reusedRejected = $false
    try {
        Stop-ValidatedProcessTree @($records | Where-Object Pid -eq 100) $reused $stopFake
    } catch {
        $reusedRejected = $true
    }
    if (-not $reusedRejected) { throw "SEC-114 reused PID self-test failed" }

    $containerRecord = [pscustomobject]@{ name=$keycloakName; id="expected-id" }
    $alienRejected = $false
    try {
        Assert-ContainerIdentity $containerRecord $keycloakName "alien-id"
    } catch {
        $alienRejected = $true
    }
    if (-not $alienRejected) { throw "SEC-114 alien container self-test failed" }

    # Existing evidence is verified byte-for-byte and never rewritten.
    $fixtureRoot = Join-Path $tempRoot ("trackflow-sec114-shutdown-test-" + [guid]::NewGuid())
    New-Item -ItemType Directory -Path $fixtureRoot | Out-Null
    try {
        $summaryPath = Join-Path $fixtureRoot "sec114-interactive-summary.json"
        $hashesPath = Join-Path $fixtureRoot "sec114-interactive-hashes.csv"
        [ordered]@{
            gate = "SEC-114-interactive-acceptance"
            secrets_recorded = $false
            http_response_bodies_recorded = $false
        } | ConvertTo-Json | Set-Content -LiteralPath $summaryPath -Encoding UTF8
        Get-FileHash -LiteralPath $summaryPath -Algorithm SHA256 |
            Select-Object Algorithm,Hash,Path |
            Export-Csv -NoTypeInformation -Encoding UTF8 -LiteralPath $hashesPath
        $beforeHash = (Get-FileHash -LiteralPath $summaryPath -Algorithm SHA256).Hash
        $beforeLength = (Get-Item -LiteralPath $hashesPath).Length
        Confirm-OrWriteInteractiveEvidence ([pscustomobject]@{
            evidence_directory = $fixtureRoot
        })
        if ((Get-FileHash -LiteralPath $summaryPath -Algorithm SHA256).Hash -ne $beforeHash -or
            (Get-Item -LiteralPath $hashesPath).Length -ne $beforeLength) {
            throw "SEC-114 evidence preservation self-test failed"
        }
    } finally {
        if (Test-Path -LiteralPath $fixtureRoot) {
            Remove-Item -LiteralPath $fixtureRoot -Recurse -Force
        }
    }
}

function Invoke-InteractiveExistingSessionAction {
    Test-ProcessOwnershipLogic
    $manifest = Read-InteractiveManifest
    $specs = @(Get-InteractiveServiceSpecs $manifest)
    switch ($InteractiveAction) {
        "Status" {
            $observability = Get-SanitizedObservability $manifest
            $status = [ordered]@{
                session_id = $manifest.session_id
                backoffice_ready = Test-OwnedListener ($specs | Where-Object Name -eq "backoffice") $manifest
                api_ready = Test-OwnedListener ($specs | Where-Object Name -eq "api") $manifest
                mcp_ready = Test-OwnedListener ($specs | Where-Object Name -eq "mcp") $manifest
                keycloak_ready = [bool](docker ps --filter "id=$($manifest.containers.keycloak.id)" --format "{{.ID}}")
                postgresql_ready = [bool](docker ps --filter "id=$($manifest.containers.postgresql.id)" --format "{{.ID}}")
                guardrail_counts = $observability.guardrail_counts
                sanitized_http_counts = $observability.sanitized_http_counts
                mcp_business_tool_counts = $observability.mcp_business_tool_counts
                mcp_legacy_unclassified_tool_events = `
                    $observability.mcp_legacy_unclassified_tool_events
                urls = $manifest.urls
            }
            $status | ConvertTo-Json -Depth 8
            return
        }
        "Credential" {
            if (-not (Test-Path -LiteralPath $credentialBlobPath -PathType Leaf)) {
                throw "Encrypted SEC-114 credential blob is missing"
            }
            $credentials = Unprotect-InteractiveCredentials
            if ($Credential -eq "Local") {
                Set-Clipboard -Value $credentials.local_password
                Write-Output "Username: mcp-qa-user@example.invalid"
            } else {
                Set-Clipboard -Value $credentials.oidc_tenant_a_password
                Write-Output "Username: sec114-tenant-a"
            }
            Write-Output "The selected ephemeral password is in the clipboard; it was not printed. Clear the clipboard after pasting."
            return
        }
        "Stop" {
            Confirm-OrWriteInteractiveEvidence $manifest
            $ownedTrees = @()
            foreach ($service in $specs) {
                $ownedTrees += @(Get-ServiceShutdownTree $service $manifest)
            }
            # Validate both exact names and immutable IDs before stopping any
            # remaining process or container. A missing --rm container is an
            # already-completed step; a reused name is always fatal.
            $containerStates = @(
                Get-ValidatedContainerState $manifest.containers.keycloak $keycloakName
                Get-ValidatedContainerState $manifest.containers.postgresql $postgresName
            )
            Stop-ValidatedProcessTree $ownedTrees
            foreach ($container in @($containerStates | Where-Object Running)) {
                docker stop $container.Name | Out-Null
                if ($LASTEXITCODE -ne 0) {
                    throw "SEC-114 container stop failed: $($container.Name)"
                }
            }
            $allInteractivePorts = @($backofficePort) + @($qaPorts)
            for ($attempt = 0; $attempt -lt 20; $attempt++) {
                $remainingListeners = @(
                    Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
                        Where-Object { $_.LocalPort -in $allInteractivePorts }
                )
                if ($remainingListeners.Count -eq 0) { break }
                Start-Sleep -Milliseconds 250
            }
            if ($remainingListeners.Count -ne 0) {
                throw "SEC-114 shutdown left an owned port in use"
            }
            Remove-Item -LiteralPath $stateDirectory -Recurse -Force
            Write-Output "SEC114_INTERACTIVE_STOPPED"
            return
        }
        default { throw "Unsupported existing-session action: $InteractiveAction" }
    }
}

function Stop-OwnedProcess($Process, [int]$Port, [string]$Marker) {
    $listeners = @(
        Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
            Where-Object { $_.LocalAddress -eq "127.0.0.1" }
    )
    if ($null -eq $Process) {
        if ($listeners.Count -gt 0) {
            throw "SEC-114 cleanup has no recorded launcher for port $Port"
        }
        return
    }
    $launcher = Get-CimInstance Win32_Process `
        -Filter "ProcessId=$($Process.Id)" -ErrorAction SilentlyContinue
    if ($null -eq $launcher) { return }
    if ($Marker -eq "next dev") {
        $launcherMarkers = @("npm", "run", "dev", "$Port")
        $serviceMarker = "next"
    } else {
        $launcherMarkers = @($Marker, "$Port")
        $serviceMarker = $Marker
    }
    if (-not (Test-CommandMarkers ([string]$launcher.CommandLine) $launcherMarkers)) {
        throw "SEC-114 cleanup launcher identity mismatch on port $Port"
    }
    $rootCreatedUtc = Convert-ProcessCreationToUtc $launcher.CreationDate
    if ($listeners.Count -gt 0) {
        if ($listeners.Count -ne 1) {
            throw "SEC-114 cleanup found multiple loopback listeners on port $Port"
        }
        Resolve-OwnedProcessChain `
            -ListenerProcessId ([int]$listeners[0].OwningProcess) `
            -ExpectedLauncherPid ([int]$Process.Id) `
            -SessionCreatedUtc $rootCreatedUtc `
            -LauncherMarkers $launcherMarkers `
            -ServiceMarker $serviceMarker | Out-Null
    }
    $ownedTree = @(Get-OwnedProcessTree ([int]$Process.Id) $rootCreatedUtc)
    Stop-ValidatedProcessTree $ownedTree
    Start-Sleep -Seconds 1
}

if ($OwnershipSelfTest) {
    Test-ProcessOwnershipLogic
    Write-Output "SEC114_PROCESS_OWNERSHIP_SELF_TEST_PASS"
    exit 0
}

if ($ObservabilitySelfTest) {
    Test-ObservabilityCountingLogic
    Write-Output "SEC114_OBSERVABILITY_SELF_TEST_PASS"
    exit 0
}

if ($ShutdownSelfTest) {
    Test-ProcessOwnershipLogic
    Test-ShutdownRecoveryLogic
    Write-Output "SEC114_SHUTDOWN_RECOVERY_SELF_TEST_PASS"
    exit 0
}

if ($Interactive) {
    Assert-InteractiveStatePath
    Assert-InteractiveEvidencePath
    if ($InteractiveAction -in @("Status", "Credential", "Stop")) {
        if ($InteractiveAction -eq "Stop" -and
            -not (Test-Path -LiteralPath $sessionManifestPath -PathType Leaf)) {
            Confirm-InteractiveAlreadyStopped
            exit 0
        }
        Invoke-InteractiveExistingSessionAction
        exit 0
    }
    Assert-Prerequisites
    if (Test-Path -LiteralPath $stateDirectory) {
        throw "SEC-114 interactive state already exists; inspect or stop that session instead of creating new credentials"
    }
    Assert-NoCollisions
    if ($InteractiveAction -eq "Validate") {
        Write-Output "SEC114_INTERACTIVE_VALIDATION_PASS"
        Write-Output "BACKOFFICE_URL=http://127.0.0.1:28080/backoffice/support"
        Write-Output "API_URL=http://127.0.0.1:28083/docs"
        Write-Output "KEYCLOAK_URL=http://127.0.0.1:28082/realms/trackflow-sec114-interactive"
        exit 0
    }
}

Push-Location $repo
try {
    Assert-Prerequisites
    if (-not $Interactive) { Assert-NoCollisions }

    New-Item -ItemType Directory -Path $stateDirectory | Out-Null
    New-Item -ItemType Directory -Path $EvidenceDirectory -Force | Out-Null

    $dbPassword = New-EphemeralSecret
    $env:TRACKFLOW_MCP_QA = "1"
    $env:TRACKFLOW_MCP_REAL_QA = "1"
    $env:TRACKFLOW_SEC114_QA = "1"
    $env:QA_KEYCLOAK_BASE_URL = "http://127.0.0.1:28082"
    $env:QA_KEYCLOAK_ADMIN_USER = "sec114-admin-" + [guid]::NewGuid().ToString("N").Substring(0, 8)
    $env:QA_KEYCLOAK_ADMIN_PASSWORD = New-EphemeralSecret
    $env:QA_KEYCLOAK_REALM = if ($Interactive) {
        "trackflow-sec114-interactive"
    } else {
        "trackflow-sec114-" + [guid]::NewGuid().ToString("N").Substring(0, 8)
    }
    $env:QA_KEYCLOAK_PUBLIC_CLIENT_ID = "mcp-playground-sec114"
    $env:QA_KEYCLOAK_MCP_CLIENT_ID = "trackflow-mcp"
    $env:QA_KEYCLOAK_MCP_CLIENT_SECRET = New-EphemeralSecret
    $env:QA_KEYCLOAK_API_CLIENT_ID = "trackflow-api"
    $env:QA_KEYCLOAK_USERNAME = "sec114-tenant-a"
    $env:QA_KEYCLOAK_PASSWORD = New-EphemeralSecret
    $env:QA_KEYCLOAK_USERNAME_B = "sec114-tenant-b"
    $env:QA_KEYCLOAK_PASSWORD_B = New-EphemeralSecret
    $env:QA_TRACKFLOW_USER_ID = [guid]::NewGuid().ToString()
    $env:QA_TRACKFLOW_USER_ID_B = [guid]::NewGuid().ToString()
    $env:QA_TRACKFLOW_LOCAL_PASSWORD = New-EphemeralSecret
    $env:QA_TRACKFLOW_LOCAL_PASSWORD_B = New-EphemeralSecret
    $env:DATABASE_URL = "postgresql+psycopg2://sec114_qa:$dbPassword@127.0.0.1:55441/sec114_qa"
    $env:TRACKFLOW_AUTH_DATABASE_PATH = Join-Path $stateDirectory "trackflow-auth.json"
    $env:JWT_SECRET_KEY = New-EphemeralSecret
    if ($Interactive) {
        $env:QA_BACKOFFICE_ORIGIN = "http://127.0.0.1:$backofficePort"
    } else {
        Remove-Item Env:QA_BACKOFFICE_ORIGIN -ErrorAction SilentlyContinue
    }

    docker run --name $keycloakName --rm -d `
        -p 127.0.0.1:28082:8080 `
        -e "KC_BOOTSTRAP_ADMIN_USERNAME=$($env:QA_KEYCLOAK_ADMIN_USER)" `
        -e "KC_BOOTSTRAP_ADMIN_PASSWORD=$($env:QA_KEYCLOAK_ADMIN_PASSWORD)" `
        quay.io/keycloak/keycloak:26.8.2 start-dev | Out-Null
    docker run --name $postgresName --rm -d `
        --tmpfs /var/lib/postgresql/data:rw,noexec,nosuid,size=256m `
        -p 127.0.0.1:55441:5432 `
        -e POSTGRES_USER=sec114_qa `
        -e "POSTGRES_PASSWORD=$dbPassword" `
        -e POSTGRES_DB=sec114_qa postgres:16 | Out-Null
    $containersStarted = $true

    for ($attempt = 0; $attempt -lt 120; $attempt++) {
        docker exec $postgresName pg_isready -U sec114_qa -d sec114_qa 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { break }
        Start-Sleep -Seconds 1
    }
    if ($LASTEXITCODE -ne 0) { throw "PostgreSQL did not become ready" }
    Wait-Http "http://127.0.0.1:28082/realms/master"

    Get-Content -Raw "infra/sql/incident_query_security.sql" |
        docker exec -i $postgresName psql -v ON_ERROR_STOP=1 -U sec114_qa -d sec114_qa | Out-Null
    Get-Content -Raw "infra/sql/agent_memory.sql" |
        docker exec -i $postgresName psql -v ON_ERROR_STOP=1 -U sec114_qa -d sec114_qa | Out-Null
    & $python "mcps/trackflow/qa/configure_keycloak.py"
    & $python "mcps/trackflow/qa/seed_trackflow.py"

    $env:TRACKFLOW_OIDC_ISSUER = "$($env:QA_KEYCLOAK_BASE_URL)/realms/$($env:QA_KEYCLOAK_REALM)"
    $env:TRACKFLOW_OIDC_JWKS_URI = "$($env:TRACKFLOW_OIDC_ISSUER)/protocol/openid-connect/certs"
    $env:TRACKFLOW_MCP_RESOURCE_URL = "http://127.0.0.1:28084/mcp"
    $env:TRACKFLOW_MCP_ISSUER = $env:TRACKFLOW_OIDC_ISSUER
    $env:TRACKFLOW_MCP_AUDIENCE = $env:QA_KEYCLOAK_MCP_CLIENT_ID
    $env:TRACKFLOW_MCP_KEYCLOAK_CLIENT_ID = $env:QA_KEYCLOAK_MCP_CLIENT_ID
    $env:TRACKFLOW_MCP_KEYCLOAK_CLIENT_SECRET = $env:QA_KEYCLOAK_MCP_CLIENT_SECRET
    $env:TRACKFLOW_API_BASE_URL = "http://127.0.0.1:28083"
    $env:TRACKFLOW_SUPPORT_MCP_URL = "http://127.0.0.1:28084/mcp"
    $env:QA_TOKEN_ENDPOINT = "$($env:TRACKFLOW_OIDC_ISSUER)/protocol/openid-connect/token"
    $env:QA_JWKS_URI = $env:TRACKFLOW_OIDC_JWKS_URI
    $env:QA_ISSUER = $env:TRACKFLOW_OIDC_ISSUER
    $env:QA_API_URL = $env:TRACKFLOW_API_BASE_URL
    $env:QA_MCP_URL = $env:TRACKFLOW_SUPPORT_MCP_URL
    $env:QA_PRM_URL = "http://127.0.0.1:28084/.well-known/oauth-protected-resource/mcp"
    $env:PYTHONPATH = "$repo;$repo\mcps\trackflow"

    $apiProcess = Start-Process -FilePath $python -ArgumentList @(
        "-m", "uvicorn", "services.api.main:app", "--host", "127.0.0.1", "--port", "28083"
    ) -WorkingDirectory $repo -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $stateDirectory "api.stdout.log") `
        -RedirectStandardError (Join-Path $stateDirectory "api.stderr.log")
    $mcpProcess = Start-Process -FilePath "uv" -ArgumentList @(
        "run", "--project", "mcps/trackflow", "--python", "3.12", "uvicorn",
        "trackflow_mcp.runtime:create_runtime_app", "--factory",
        "--host", "127.0.0.1", "--port", "28084"
    ) -WorkingDirectory $repo -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $stateDirectory "mcp.stdout.log") `
        -RedirectStandardError (Join-Path $stateDirectory "mcp.stderr.log")
    Wait-Http "http://127.0.0.1:28083/docs"
    Wait-Http $env:QA_PRM_URL

    if ($Interactive) {
        $env:API_BACKEND_URL = "http://127.0.0.1:28083"
        $env:NEXT_PUBLIC_SUPPORT_OIDC_AUTHORIZATION_ENDPOINT = "$($env:TRACKFLOW_OIDC_ISSUER)/protocol/openid-connect/auth"
        $env:NEXT_PUBLIC_SUPPORT_OIDC_TOKEN_ENDPOINT = $env:QA_TOKEN_ENDPOINT
        $env:NEXT_PUBLIC_SUPPORT_OIDC_CLIENT_ID = $env:QA_KEYCLOAK_PUBLIC_CLIENT_ID
        Remove-Item Env:NEXT_PUBLIC_API_URL -ErrorAction SilentlyContinue
        $backofficeProcess = Start-Process -FilePath "npm.cmd" -ArgumentList @(
            "run", "dev", "--", "--hostname", "127.0.0.1", "--port", "$backofficePort"
        ) -WorkingDirectory (Join-Path $repo "uis\backoffice") -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $stateDirectory "backoffice.stdout.log") `
            -RedirectStandardError (Join-Path $stateDirectory "backoffice.stderr.log")
        Wait-Http "http://127.0.0.1:$backofficePort/login"

        Protect-InteractiveCredentials @{
            local_password = $env:QA_TRACKFLOW_LOCAL_PASSWORD
            oidc_tenant_a_password = $env:QA_KEYCLOAK_PASSWORD
        }
        $keycloakId = docker inspect --format "{{.Id}}" $keycloakName
        $postgresId = docker inspect --format "{{.Id}}" $postgresName
        [ordered]@{
            schema_version = 1
            mode = "sec114-interactive"
            session_id = [guid]::NewGuid().ToString()
            repository = $repo
            created_at_utc = [DateTime]::UtcNow.ToString("o")
            evidence_directory = [IO.Path]::GetFullPath($EvidenceDirectory)
            urls = [ordered]@{
                backoffice = "http://127.0.0.1:$backofficePort/backoffice/support"
                api = "http://127.0.0.1:28083/docs"
                keycloak = $env:TRACKFLOW_OIDC_ISSUER
                mcp = $env:QA_MCP_URL
            }
            processes = [ordered]@{
                backoffice = @{
                    pid = $backofficeProcess.Id
                    port = $backofficePort
                    created_utc = (Convert-ProcessCreationToUtc (
                        Get-CimInstance Win32_Process -Filter "ProcessId=$($backofficeProcess.Id)"
                    ).CreationDate).ToString("o")
                }
                api = @{
                    pid = $apiProcess.Id
                    port = 28083
                    created_utc = (Convert-ProcessCreationToUtc (
                        Get-CimInstance Win32_Process -Filter "ProcessId=$($apiProcess.Id)"
                    ).CreationDate).ToString("o")
                }
                mcp = @{
                    pid = $mcpProcess.Id
                    port = 28084
                    created_utc = (Convert-ProcessCreationToUtc (
                        Get-CimInstance Win32_Process -Filter "ProcessId=$($mcpProcess.Id)"
                    ).CreationDate).ToString("o")
                }
            }
            containers = [ordered]@{
                keycloak = @{ name = $keycloakName; id = $keycloakId }
                postgresql = @{ name = $postgresName; id = $postgresId }
            }
            logs = [ordered]@{
                api_stderr = Join-Path $stateDirectory "api.stderr.log"
                mcp_stdout = Join-Path $stateDirectory "mcp.stdout.log"
                mcp_stderr = Join-Path $stateDirectory "mcp.stderr.log"
                backoffice_stderr = Join-Path $stateDirectory "backoffice.stderr.log"
            }
        } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $sessionManifestPath -Encoding UTF8
        $preserveInteractive = $true
        Write-Output "SEC114_INTERACTIVE_READY"
        Write-Output "BACKOFFICE_URL=http://127.0.0.1:$backofficePort/backoffice/support"
        Write-Output "API_URL=http://127.0.0.1:28083/docs"
        Write-Output "KEYCLOAK_URL=$($env:TRACKFLOW_OIDC_ISSUER)"
        Write-Output "Use -Interactive -InteractiveAction Credential to place an ephemeral password in the clipboard."
        Write-Output "Use -Interactive -InteractiveAction Stop only after acceptance is complete."
        return
    }

    $junit = Join-Path $EvidenceDirectory "sec114-real-e2e.junit.xml"
    & $python -m pytest "mcps/trackflow/qa/test_e2e.py" -q -p no:cacheprovider "--junitxml=$junit"
    if ($LASTEXITCODE -ne 0) {
        Write-Output "SEC114_API_STDERR_TAIL"
        Get-Content (Join-Path $stateDirectory "api.stderr.log") -Tail 120
        Write-Output "SEC114_MCP_STDERR_TAIL"
        Get-Content (Join-Path $stateDirectory "mcp.stderr.log") -Tail 120
        throw "SEC-114 real E2E failed"
    }

    $hash = (Get-FileHash -Algorithm SHA256 $junit).Hash.ToLowerInvariant()
    [ordered]@{
        gate = "SEC-114-real-e2e"
        result = "pass"
        transport = "OIDC-PKCE-to-Support-to-MCP-to-API-to-PostgreSQL"
        tenants = 2
        tracking_owner_case = "45821"
        duplicate_noninterference = $true
        scope_enforcement = $true
        projection = "recorded_dispatch"
        junit_sha256 = $hash
    } | ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $EvidenceDirectory "sec114-real-e2e-summary.json")
    Get-FileHash -Algorithm SHA256 `
        (Join-Path $EvidenceDirectory "sec114-real-e2e-summary.json") |
        Select-Object Algorithm, Hash, Path |
        Export-Csv -NoTypeInformation -Encoding UTF8 `
            (Join-Path $EvidenceDirectory "sec114-real-e2e-hashes.csv")
    Write-Output "SEC114_REAL_E2E_PASS"
    Write-Output "JUNIT_SHA256=$hash"
} finally {
    if (-not $preserveInteractive) {
        if ($Interactive) {
            Stop-OwnedProcess $backofficeProcess $backofficePort "next dev"
        }
        Stop-OwnedProcess $mcpProcess 28084 "trackflow_mcp.runtime:create_runtime_app"
        Stop-OwnedProcess $apiProcess 28083 "services.api.main:app"
        if ($containersStarted) {
            docker stop $keycloakName $postgresName | Out-Null
        }
        if ([IO.Path]::GetFullPath($stateDirectory).StartsWith($tempRoot) -and
            (Test-Path $stateDirectory)) {
            Remove-Item -LiteralPath $stateDirectory -Recurse -Force
        }
    }
    Pop-Location
}
