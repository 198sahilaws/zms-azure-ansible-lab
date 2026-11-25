<#
.SYNOPSIS
    Generates Table 5 flows F5 (SSH) and F6 (RDP) from the bastion, plus optional negative probes.

.DESCRIPTION
    Run on the Windows bastion. For every target it opens a real protocol handshake and then
    disconnects. No login is attempted and no credentials are used.

      SSH (F5)  Reads the server banner and sends a client identification string.
                sshd logs "Connection closed by <bastion> ... [preauth]", which is expected.
      RDP (F6)  Sends an X.224 Connection Request carrying an RDP Negotiation Request and reads
                the server's Connection Confirm (reports the protocol it selected, e.g. NLA).
      Negative  Plain TCP connect to ports the bastion must NOT reach once ZMS policy is
                enforced: the app ports on the Linux tiers, and WinRM/SMB on the DCs.

    Each probe ends as OPEN, REFUSED (reset), TIMEOUT (silently dropped) or ERROR, with a verdict:
      PASS  matches what Table 5 expects
      FAIL  an allowed flow did not connect, or (with -Enforced) a denied flow connected
      INFO  a denied flow connected before enforcement (normal until policy is enforced)

    Do NOT use -Mode Negative or All while ZMS is learning flows for policy recommendations:
    the denied flows would land in the same flow data as the real ones.

    Runs on Windows PowerShell 5.1 (Windows Server 2022) and PowerShell 7. No modules needed.

.PARAMETER SshTargets
    F5 targets: name -> private IP of each Linux host (web, middleware, db, client, control node).
    The part of a name before the first '-' is its role, used to pick negative ports.

.PARAMETER RdpTargets
    F6 targets: name -> private IP of each Windows host in the Infra RG.
    Default: the web-app-adds static DC addresses on the default VNet 10.193.0.0/16.

.PARAMETER NegativePorts
    role -> ports the bastion must not reach. Used by -Mode Negative and All.

.EXAMPLE
    # Interactive use on the bastion (hashtables cannot be passed through powershell.exe -File)
    Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
    .\flow-bastion.ps1 -SshTargets @{ web='10.193.10.4'; middleware='10.193.10.5'; db='10.193.10.6';
                                      client='10.193.40.4'; ansible_control='10.193.60.4' }

.EXAMPLE
    # After enforcement: allowed and denied flows, fail on any mismatch
    .\flow-bastion.ps1 -SshTargets $targets -Mode All -Enforced

.EXAMPLE
    # A burst for the learning window: 6 rounds, 30 s apart
    .\flow-bastion.ps1 -SshTargets $targets -Rounds 6 -IntervalSeconds 30
#>
[CmdletBinding()]
param(
    [System.Collections.IDictionary]$SshTargets = @{},

    [System.Collections.IDictionary]$RdpTargets = @{ dc = '10.193.50.10'; rodc = '10.193.50.12' },

    [System.Collections.IDictionary]$NegativePorts = @{
        web        = @(8090)
        middleware = @(8091)
        db         = @(3306)
        dc         = @(5986, 445)
        adc        = @(5986, 445)
        rodc       = @(5986, 445)
    },

    [ValidateSet('Positive', 'Negative', 'All')]
    [string]$Mode = 'Positive',

    [switch]$Enforced,

    [ValidateRange(1, 100)]
    [int]$Rounds = 1,

    [ValidateRange(0, 3600)]
    [int]$IntervalSeconds = 30,

    [ValidateRange(500, 60000)]
    [int]$TimeoutMs = 5000,

    # Emit result objects instead of a table and exit code (used by the Ansible playbook).
    [switch]$PassThru
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

function Connect-Tcp {
    param([string]$Address, [int]$Port)

    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync($Address, $Port)
        if (-not $task.Wait($TimeoutMs)) {
            $client.Close()
            return @{ Status = 'TIMEOUT'; Client = $null; Detail = "no answer in $TimeoutMs ms" }
        }
        $client.ReceiveTimeout = $TimeoutMs
        $client.SendTimeout = $TimeoutMs
        return @{ Status = 'OPEN'; Client = $client; Detail = 'TCP connect' }
    }
    catch {
        $client.Close()
        $ex = $_.Exception
        while ($null -ne $ex.InnerException) { $ex = $ex.InnerException }
        if ($ex -is [System.Net.Sockets.SocketException]) {
            switch ([string]$ex.SocketErrorCode) {
                'ConnectionRefused' { return @{ Status = 'REFUSED'; Client = $null; Detail = 'connection refused (reset)' } }
                'TimedOut'          { return @{ Status = 'TIMEOUT'; Client = $null; Detail = 'timed out' } }
                default             { return @{ Status = 'ERROR';   Client = $null; Detail = [string]$ex.SocketErrorCode } }
            }
        }
        return @{ Status = 'ERROR'; Client = $null; Detail = $ex.Message }
    }
}

function Test-SshHandshake {
    param([System.Net.Sockets.TcpClient]$Client)

    $stream = $Client.GetStream()
    $reader = New-Object System.IO.StreamReader -ArgumentList $stream, ([System.Text.Encoding]::ASCII)
    $banner = $null
    # RFC 4253 allows other lines before the version string; read a few at most.
    for ($i = 0; $i -lt 5 -and $null -eq $banner; $i++) {
        $line = $reader.ReadLine()
        if ($null -eq $line) { break }
        if ($line.StartsWith('SSH-')) { $banner = $line }
    }
    if ($null -eq $banner) { throw 'no SSH version banner received' }

    $ident = [System.Text.Encoding]::ASCII.GetBytes("SSH-2.0-ZMS_FlowProbe`r`n")
    $stream.Write($ident, 0, $ident.Length)
    return "SSH banner: $banner"
}

function Test-RdpHandshake {
    param([System.Net.Sockets.TcpClient]$Client)

    $stream = $Client.GetStream()
    # TPKT (4) + X.224 Connection Request (7) + RDP_NEG_REQ (8) = 19 bytes.
    # requestedProtocols = TLS | CredSSP (0x00000003).
    [byte[]]$request = 0x03, 0x00, 0x00, 0x13,
                       0x0E, 0xE0, 0x00, 0x00, 0x00, 0x00, 0x00,
                       0x01, 0x00, 0x08, 0x00, 0x03, 0x00, 0x00, 0x00
    $stream.Write($request, 0, $request.Length)

    $buffer = New-Object byte[] 64
    $read = $stream.Read($buffer, 0, $buffer.Length)
    if ($read -lt 7 -or $buffer[0] -ne 0x03 -or $buffer[5] -ne 0xD0) {
        throw "no X.224 Connection Confirm ($read bytes received)"
    }

    if ($read -ge 19 -and $buffer[11] -eq 0x02) {
        $names = @{ 0 = 'standard RDP'; 1 = 'TLS'; 2 = 'CredSSP (NLA)'; 4 = 'RDSTLS'; 8 = 'CredSSP early auth' }
        $proto = [int][BitConverter]::ToUInt32($buffer, 15)
        $label = if ($names.ContainsKey($proto)) { $names[$proto] } else { "protocol $proto" }
        return "RDP connection confirm, server selected $label"
    }
    if ($read -ge 19 -and $buffer[11] -eq 0x03) {
        return "RDP connection confirm, negotiation failure code $([BitConverter]::ToUInt32($buffer, 15))"
    }
    return 'RDP connection confirm'
}

# ---- Build the probe plan -------------------------------------------------------------
$plan = New-Object System.Collections.Generic.List[object]

if ($Mode -ne 'Negative') {
    foreach ($name in @($SshTargets.Keys)) {
        $plan.Add([pscustomobject]@{ Flow = 'F5'; Target = [string]$name; Address = [string]$SshTargets[$name]; Port = 22; Probe = 'ssh'; Expect = 'allow' })
    }
    foreach ($name in @($RdpTargets.Keys)) {
        $plan.Add([pscustomobject]@{ Flow = 'F6'; Target = [string]$name; Address = [string]$RdpTargets[$name]; Port = 3389; Probe = 'rdp'; Expect = 'allow' })
    }
}

if ($Mode -ne 'Positive') {
    $everyone = @{}
    foreach ($name in @($SshTargets.Keys)) { $everyone[[string]$name] = [string]$SshTargets[$name] }
    foreach ($name in @($RdpTargets.Keys)) { $everyone[[string]$name] = [string]$RdpTargets[$name] }
    foreach ($name in @($everyone.Keys)) {
        $role = ($name -split '-')[0]
        if (@($NegativePorts.Keys) -contains $role) {
            foreach ($port in @($NegativePorts[$role])) {
                $plan.Add([pscustomobject]@{ Flow = 'N6'; Target = $name; Address = $everyone[$name]; Port = [int]$port; Probe = 'tcp'; Expect = 'deny' })
            }
        }
    }
    if (-not $Enforced) {
        Write-Warning 'Negative probes before enforcement: every one should connect (INFO). Do not run them while ZMS is learning flows.'
    }
}

if ($plan.Count -eq 0) {
    throw 'Nothing to probe: pass -SshTargets and/or -RdpTargets.'
}
$plan = @($plan | Sort-Object Flow, Target, Port)

# ---- Run ---------------------------------------------------------------------------------
$results = New-Object System.Collections.Generic.List[object]

for ($round = 1; $round -le $Rounds; $round++) {
    foreach ($p in $plan) {
        $timer = [System.Diagnostics.Stopwatch]::StartNew()
        $conn = Connect-Tcp -Address $p.Address -Port $p.Port
        $detail = $conn.Detail

        if ($null -ne $conn.Client) {
            try {
                switch ($p.Probe) {
                    'ssh'   { $detail = Test-SshHandshake -Client $conn.Client }
                    'rdp'   { $detail = Test-RdpHandshake -Client $conn.Client }
                    default { $detail = 'TCP connect' }
                }
            }
            catch {
                $detail = "TCP connected; handshake failed: $($_.Exception.Message)"
            }
            finally {
                $conn.Client.Close()
            }
        }
        $timer.Stop()

        if ($p.Expect -eq 'allow') {
            $verdict = if ($conn.Status -eq 'OPEN') { 'PASS' } else { 'FAIL' }
        }
        elseif ($conn.Status -eq 'OPEN') {
            $verdict = if ($Enforced) { 'FAIL' } else { 'INFO' }
        }
        else {
            $verdict = 'PASS'
        }

        $results.Add([pscustomobject]@{
            Time    = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ssK')
            Round   = $round
            Flow    = $p.Flow
            Source  = $env:COMPUTERNAME
            Target  = $p.Target
            Address = $p.Address
            Port    = $p.Port
            Expect  = $p.Expect
            Result  = $conn.Status
            Verdict = $verdict
            Ms      = [int]$timer.ElapsedMilliseconds
            Detail  = $detail
        })
    }
    if ($round -lt $Rounds -and $IntervalSeconds -gt 0) {
        Start-Sleep -Seconds $IntervalSeconds
    }
}

# ---- Report ------------------------------------------------------------------------------
if ($PassThru) {
    $results
    return
}

$results |
    Format-Table Round, Flow, Target, Address, Port, Expect, Result, Verdict, Ms, Detail -AutoSize |
    Out-String -Width 250 |
    Write-Host

$failed = @($results | Where-Object { $_.Verdict -eq 'FAIL' })
$info   = @($results | Where-Object { $_.Verdict -eq 'INFO' })
Write-Host ("{0} probes: {1} passed, {2} failed, {3} denied-but-open (not enforced yet)" -f `
    $results.Count, @($results | Where-Object { $_.Verdict -eq 'PASS' }).Count, $failed.Count, $info.Count)

if ($failed.Count -gt 0) { exit 1 }
