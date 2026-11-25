<#
.SYNOPSIS
    Runs a list of network probes from this Windows host and returns one result object per probe.

.DESCRIPTION
    Windows counterpart of flow-probe.py, used by the flow-test playbooks on the domain
    controllers through ansible.windows.win_powershell. No modules needed; runs on Windows
    PowerShell 5.1 (Windows Server 2022) and PowerShell 7.

    Each probe opens a real TCP connection from this host, then:
      tcp    nothing more
      ssh    reads the server banner, sends a client identification string (no login)
      rdp    X.224 Connection Request + RDP negotiation, reads the Connection Confirm (no login)
      http   one GET, reads the status line
      https  TLS handshake (certificate checked unless verify = $false), then one GET

    Result: OPEN, REFUSED (reset), TIMEOUT (dropped) or ERROR (DNS, unreachable...).
    Verdict: allow -> PASS if OPEN else FAIL; deny -> PASS unless OPEN, which is INFO before
    enforcement and FAIL with -Enforced.

.PARAMETER Plan
    List of probes. Keys: flow, target, expect ('allow' | 'deny'), probe, and either
    address + port (+ path for http) or url. Optional: verify (https), zscaler (parse ip.zscaler.com).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [object[]]$Plan,

    [ValidateRange(500, 60000)]
    [int]$TimeoutMs = 5000,

    [ValidateRange(1, 100)]
    [int]$Rounds = 1,

    [ValidateRange(0, 3600)]
    [int]$IntervalSeconds = 30,

    [switch]$Enforced
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

function Get-Field {
    param($Item, [string]$Name, $Default = $null)
    if ($Item -is [System.Collections.IDictionary]) {
        if (@($Item.Keys) -contains $Name) { return $Item[$Name] }
        return $Default
    }
    $prop = $Item.PSObject.Properties[$Name]
    if ($null -ne $prop) { return $prop.Value }
    return $Default
}

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
                'ConnectionReset'   { return @{ Status = 'REFUSED'; Client = $null; Detail = 'connection reset' } }
                'TimedOut'          { return @{ Status = 'TIMEOUT'; Client = $null; Detail = 'timed out' } }
                'HostNotFound'      { return @{ Status = 'ERROR';   Client = $null; Detail = "DNS: $Address not found" } }
                default             { return @{ Status = 'ERROR';   Client = $null; Detail = [string]$ex.SocketErrorCode } }
            }
        }
        return @{ Status = 'ERROR'; Client = $null; Detail = $ex.Message }
    }
}

function Read-Bytes {
    param([System.IO.Stream]$Stream, [int]$Limit)
    $ms = New-Object System.IO.MemoryStream
    $buffer = New-Object byte[] 65536
    try {
        while ($ms.Length -lt $Limit) {
            $n = $Stream.Read($buffer, 0, [Math]::Min($buffer.Length, $Limit - [int]$ms.Length))
            if ($n -le 0) { break }
            $ms.Write($buffer, 0, $n)
        }
    }
    catch {
        # A read timeout after some data just ends the response; with no data it is a failure.
        if ($ms.Length -eq 0) { throw 'no reply before timeout' }
    }
    return , $ms.ToArray()
}

function Invoke-SshHandshake {
    param([System.IO.Stream]$Stream)
    $reader = New-Object System.IO.StreamReader -ArgumentList $Stream, ([System.Text.Encoding]::ASCII)
    $banner = $null
    for ($i = 0; $i -lt 5 -and $null -eq $banner; $i++) {
        $line = $reader.ReadLine()
        if ($null -eq $line) { break }
        if ($line.StartsWith('SSH-')) { $banner = $line }
    }
    if ($null -eq $banner) { throw 'no SSH version banner received' }
    $ident = [System.Text.Encoding]::ASCII.GetBytes("SSH-2.0-ZMS_FlowProbe`r`n")
    $Stream.Write($ident, 0, $ident.Length)
    return "SSH banner: $banner"
}

function Invoke-RdpHandshake {
    param([System.IO.Stream]$Stream)
    [byte[]]$request = 0x03, 0x00, 0x00, 0x13,
                       0x0E, 0xE0, 0x00, 0x00, 0x00, 0x00, 0x00,
                       0x01, 0x00, 0x08, 0x00, 0x03, 0x00, 0x00, 0x00
    $Stream.Write($request, 0, $request.Length)
    $buffer = New-Object byte[] 64
    $read = $Stream.Read($buffer, 0, $buffer.Length)
    if ($read -lt 7 -or $buffer[0] -ne 0x03 -or $buffer[5] -ne 0xD0) {
        throw "no X.224 Connection Confirm ($read bytes received)"
    }
    if ($read -ge 19 -and $buffer[11] -eq 0x02) {
        $names = @{ 0 = 'standard RDP'; 1 = 'TLS'; 2 = 'CredSSP (NLA)'; 4 = 'RDSTLS'; 8 = 'CredSSP early auth' }
        $proto = [int][BitConverter]::ToUInt32($buffer, 15)
        $label = if ($names.ContainsKey($proto)) { $names[$proto] } else { "protocol $proto" }
        return "RDP connection confirm, server selected $label"
    }
    return 'RDP connection confirm'
}

function Invoke-HttpGet {
    param([System.IO.Stream]$Stream, [string]$HostHeader, [string]$Path, [bool]$WantBody)
    $request = "GET $Path HTTP/1.1`r`nHost: $HostHeader`r`nUser-Agent: zms-flow-probe/1.0`r`nAccept: */*`r`nConnection: close`r`n`r`n"
    $bytes = [System.Text.Encoding]::ASCII.GetBytes($request)
    $Stream.Write($bytes, 0, $bytes.Length)
    $limit = if ($WantBody) { 262144 } else { 8192 }
    $data = Read-Bytes -Stream $Stream -Limit $limit
    $text = [System.Text.Encoding]::GetEncoding('iso-8859-1').GetString($data)
    $split = $text.IndexOf("`r`n`r`n")
    $head = if ($split -ge 0) { $text.Substring(0, $split) } else { $text }
    $body = if ($split -ge 0) { $text.Substring($split + 4) } else { '' }
    $lines = $head -split "`r`n"
    if (-not $lines[0].StartsWith('HTTP/')) { throw 'no HTTP status line' }
    $detail = $lines[0]
    foreach ($l in $lines) {
        if ($l -match '^(?i)location:\s*(.+)$') { $detail += " -> $($Matches[1])"; break }
    }
    return @{ Detail = $detail; Body = $body }
}

function Get-ZscalerStatus {
    param([string]$Body)
    $text = ($Body -replace '<[^>]+>', ' ') -replace '\s+', ' '
    if ($text -match '(?i)not going through the Zscaler') { $v = 'not via Zscaler' }
    elseif ($text -match '(?i)(accessing this host via|going through) (a |the )?Zscaler') { $v = 'via Zscaler' }
    else { $v = 'Zscaler status not recognised' }
    if ($text -match '(?i)IP address[^0-9]{0,40}(\d{1,3}(\.\d{1,3}){3})') { $v += ", egress IP $($Matches[1])" }
    return $v
}

function Invoke-Probe {
    param($P)

    $kind = [string](Get-Field $P 'probe' 'tcp')
    $path = [string](Get-Field $P 'path' '/')
    $url = Get-Field $P 'url'
    if ($url) {
        $uri = [System.Uri]$url
        $kind = $uri.Scheme
        $address = $uri.Host
        $port = $uri.Port
        $path = $uri.PathAndQuery
        $hostHeader = if ($uri.IsDefaultPort) { $uri.Host } else { "$($uri.Host):$($uri.Port)" }
    }
    else {
        $address = [string](Get-Field $P 'address')
        $port = [int](Get-Field $P 'port')
        $hostHeader = "${address}:$port"
    }

    $conn = Connect-Tcp -Address $address -Port $port
    $detail = $conn.Detail
    if ($null -ne $conn.Client) {
        $stream = $conn.Client.GetStream()
        try {
            switch ($kind) {
                'ssh'   { $detail = Invoke-SshHandshake -Stream $stream }
                'rdp'   { $detail = Invoke-RdpHandshake -Stream $stream }
                'http'  { $detail = (Invoke-HttpGet -Stream $stream -HostHeader $hostHeader -Path $path -WantBody $false).Detail }
                'https' {
                    $verify = [bool](Get-Field $P 'verify' $true)
                    if ($verify) {
                        $tls = New-Object System.Net.Security.SslStream -ArgumentList $stream, $false
                    }
                    else {
                        $accept = [System.Net.Security.RemoteCertificateValidationCallback] { $true }
                        $tls = New-Object System.Net.Security.SslStream -ArgumentList $stream, $false, $accept
                    }
                    $tls.ReadTimeout = $TimeoutMs
                    $tls.WriteTimeout = $TimeoutMs
                    try {
                        $tls.AuthenticateAsClient($address)
                    }
                    catch {
                        $inner = $_.Exception
                        while ($null -ne $inner.InnerException) { $inner = $inner.InnerException }
                        $detail = "TCP connected; TLS failed: $($inner.Message)"
                        break
                    }
                    $zs = [bool](Get-Field $P 'zscaler' $false)
                    $r = Invoke-HttpGet -Stream $tls -HostHeader $hostHeader -Path $path -WantBody $zs
                    $detail = "$($tls.SslProtocol) $($r.Detail)"
                    if ($zs) { $detail += "; " + (Get-ZscalerStatus -Body $r.Body) }
                    $tls.Dispose()
                }
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
    return @{ Address = $address; Port = $port; Status = $conn.Status; Detail = $detail }
}

$results = New-Object System.Collections.Generic.List[object]
for ($round = 1; $round -le $Rounds; $round++) {
    foreach ($p in $Plan) {
        $timer = [System.Diagnostics.Stopwatch]::StartNew()
        $r = Invoke-Probe -P $p
        $timer.Stop()
        $expect = [string](Get-Field $p 'expect' 'allow')
        if ($expect -eq 'allow') {
            $verdict = if ($r.Status -eq 'OPEN') { 'PASS' } else { 'FAIL' }
        }
        elseif ($r.Status -eq 'OPEN') {
            $verdict = if ($Enforced) { 'FAIL' } else { 'INFO' }
        }
        else {
            $verdict = 'PASS'
        }
        $results.Add([pscustomobject]@{
            Time    = (Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz')
            Round   = $round
            Flow    = [string](Get-Field $p 'flow' '')
            Source  = $env:COMPUTERNAME
            Target  = [string](Get-Field $p 'target' $r.Address)
            Address = $r.Address
            Port    = $r.Port
            Expect  = $expect
            Result  = $r.Status
            Verdict = $verdict
            Ms      = [int]$timer.ElapsedMilliseconds
            Detail  = $r.Detail
        })
    }
    if ($round -lt $Rounds -and $IntervalSeconds -gt 0) { Start-Sleep -Seconds $IntervalSeconds }
}
$results
