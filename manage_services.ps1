[CmdletBinding()]
param(
    # Start：只补齐尚未运行的服务；Restart：停止后全部重启；
    # Stop：停止全部服务；Status：只查看状态，不改变任何进程。
    [ValidateSet("Start", "Restart", "Stop", "Status")]
    [string]$Action = "Restart"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

# 用脚本自身的位置确定项目目录，所以从任意 PowerShell 目录运行都可以。
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogDirectory = Join-Path $ProjectRoot "data\runtime_logs"
$PythonExecutable = (Get-Command python -ErrorAction Stop).Source

# 端口、入口文件和 HTTP 健康地址共同描述一个服务。
# 停止进程前仍要求端口和入口文件同时匹配，避免误杀其他软件；
# 判断“能不能使用”时还会请求 HealthPath，避免把只占着端口的僵死服务当成正常服务。
$Services = @(
    [pscustomobject]@{ Name = "usb6363-core"; Port = 8765; Script = "usb6363_server.py"; HealthPath = "/health" };
    [pscustomobject]@{ Name = "two-peak-viewer"; Port = 8766; Script = "two_peak_viewer.py"; HealthPath = "/" };
    [pscustomobject]@{ Name = "power-drift-webui"; Port = 8767; Script = "power_drift_webui.py"; HealthPath = "/api/status" };
    [pscustomobject]@{ Name = "ai-stream-console"; Port = 8768; Script = "ai_stream_console.py"; HealthPath = "/api/status" }
)

function Get-PortProcessInfo {
    param([Parameter(Mandatory)]$Service)

    $listener = Get-NetTCPConnection `
        -LocalPort $Service.Port `
        -State Listen `
        -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $listener) {
        return $null
    }

    $processInfo = Get-CimInstance `
        Win32_Process `
        -Filter "ProcessId=$($listener.OwningProcess)" `
        -ErrorAction SilentlyContinue
    $commandLine = if ($null -eq $processInfo) { "" } else { [string]$processInfo.CommandLine }
    return [pscustomobject]@{
        ProcessId = [int]$listener.OwningProcess
        CommandLine = $commandLine
        Expected = $commandLine -like "*$($Service.Script)*"
    }
}

function Test-PortListening {
    param([Parameter(Mandatory)][int]$Port)

    return $null -ne (Get-NetTCPConnection `
        -LocalPort $Port `
        -State Listen `
        -ErrorAction SilentlyContinue | Select-Object -First 1)
}

function Test-ServiceHealthy {
    param(
        [Parameter(Mandatory)]$Service,
        [int]$TimeoutSeconds = 2
    )

    try {
        # 直接请求 HTTP，不先依赖 Get-NetTCPConnection。这样即使当前 PowerShell
        # 没有读取系统 TCP 表的权限，也仍然能判断网页服务是否真的可用。
        # 这里不解析具体业务字段；只要服务能及时返回 2xx，就说明 HTTP 线程仍能处理请求。
        $response = Invoke-WebRequest `
            -UseBasicParsing `
            -Uri "http://127.0.0.1:$($Service.Port)$($Service.HealthPath)" `
            -TimeoutSec $TimeoutSeconds
        return [int]$response.StatusCode -ge 200 -and [int]$response.StatusCode -lt 300
    }
    catch {
        return $false
    }
}

function Invoke-OptionalPost {
    param(
        [Parameter(Mandatory)][string]$Url,
        [hashtable]$Body = @{},
        [int]$TimeoutSeconds = 6
    )

    try {
        $json = $Body | ConvertTo-Json -Depth 8
        Invoke-RestMethod `
            -Method Post `
            -Uri $Url `
            -ContentType "application/json; charset=utf-8" `
            -Body ([System.Text.Encoding]::UTF8.GetBytes($json)) `
            -TimeoutSec $TimeoutSeconds | Out-Null
        Write-Host "[OK] POST $Url"
    }
    catch {
        # 停止接口均设计为尽力而为。服务未运行或任务已经停止时，不阻止后续进程退出。
        Write-Warning "POST $Url failed: $($_.Exception.Message)"
    }
}

function Get-UnifiedRestoreSettings {
    # 只保存健康运行中的统一流。已经停止或带错误的流不会被自动重新启动。
    $coreService = $Services | Where-Object { $_.Port -eq 8765 } | Select-Object -First 1
    if (-not (Test-ServiceHealthy -Service $coreService)) {
        if (Test-PortListening -Port 8765) {
            Write-Warning "usb6363-core is listening, but its HTTP API is unresponsive; unified settings cannot be restored."
        }
        return $null
    }

    try {
        $status = Invoke-RestMethod `
            -Uri "http://127.0.0.1:8765/api/ai/unified/status" `
            -TimeoutSec 5
        if (-not [bool]$status.running) {
            return $null
        }
        $settings = $status.settings
        return [ordered]@{
            channels = @($settings.channels)
            samples_per_frame = [int]$settings.samples_per_frame
            rate = [double]$settings.rate_per_channel
            terminal_config = [string]$settings.terminal_config
            min_val = [double]$settings.min_val
            max_val = [double]$settings.max_val
            timeout = [double]$settings.timeout
            trigger_enabled = [bool]$settings.trigger_enabled
            trigger_source = [string]$settings.trigger_source
            trigger_edge = [string]$settings.trigger_edge
            resync_every_frames = [int]$settings.resync_every_frames
        }
    }
    catch {
        Write-Warning "Could not read unified stream settings: $($_.Exception.Message)"
        return $null
    }
}

function Stop-RunningTasks {
    # 先停可能写 AO 的任务，再停记录器，最后才释放底层 AI task。
    $viewerService = $Services | Where-Object { $_.Port -eq 8766 } | Select-Object -First 1
    $powerService = $Services | Where-Object { $_.Port -eq 8767 } | Select-Object -First 1
    $coreService = $Services | Where-Object { $_.Port -eq 8765 } | Select-Object -First 1

    if (Test-ServiceHealthy -Service $viewerService) {
        Invoke-OptionalPost -Url "http://127.0.0.1:8766/api/power_lock/stop"
        Invoke-OptionalPost -Url "http://127.0.0.1:8766/api/ao_scan/stop"
        Invoke-OptionalPost -Url "http://127.0.0.1:8766/api/test_sync/stop"
        Invoke-OptionalPost -Url "http://127.0.0.1:8766/api/trend/stop"
    }
    if (Test-ServiceHealthy -Service $powerService) {
        Invoke-OptionalPost -Url "http://127.0.0.1:8767/api/stop" -TimeoutSeconds 12
    }
    if (Test-ServiceHealthy -Service $coreService) {
        Invoke-OptionalPost -Url "http://127.0.0.1:8765/api/ai/unified/stop" -TimeoutSeconds 12
        Invoke-OptionalPost -Url "http://127.0.0.1:8765/api/ai/frame_stream/stop" -TimeoutSeconds 12
        Invoke-OptionalPost -Url "http://127.0.0.1:8765/api/ai/clear" -TimeoutSeconds 12
    }
}

function Stop-LabServices {
    Stop-RunningTasks

    # 从上层 UI 向底层 core 逆序停止，避免上层在 core 退出后继续轮询产生噪声。
    foreach ($service in @($Services | Sort-Object Port -Descending)) {
        $info = Get-PortProcessInfo -Service $service
        if ($null -eq $info) {
            Write-Host "[--] $($service.Name) is already stopped."
            continue
        }
        if (-not $info.Expected) {
            throw "Port $($service.Port) is owned by an unexpected process: $($info.CommandLine)"
        }

        Stop-Process -Id $info.ProcessId -Force
        $processObject = Get-Process -Id $info.ProcessId -ErrorAction SilentlyContinue
        if ($null -ne $processObject) {
            $processObject.WaitForExit(5000) | Out-Null
        }
        Write-Host "[OK] Stopped $($service.Name) (PID $($info.ProcessId))."
    }
}

function Wait-ServiceReady {
    param(
        [Parameter(Mandatory)]$Service,
        [int]$TimeoutSeconds = 12
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-ServiceHealthy -Service $Service) {
            return $true
        }
        Start-Sleep -Milliseconds 150
    }
    return $false
}

function Start-LabService {
    param([Parameter(Mandatory)]$Service)

    $existing = Get-PortProcessInfo -Service $Service
    if ($null -ne $existing) {
        if (-not $existing.Expected) {
            throw "Port $($Service.Port) is owned by an unexpected process: $($existing.CommandLine)"
        }
    }

    if (Test-ServiceHealthy -Service $Service) {
        $pidText = if ($null -eq $existing) { "unknown" } else { [string]$existing.ProcessId }
        Write-Host "[--] $($Service.Name) is already running (PID $pidText)."
        return
    }

    if ($null -ne $existing) {
        # 入口文件正确但 HTTP 不响应，说明这个进程已经不能继续提供服务。
        # Start 也会修复这种“假运行”状态，不必要求用户先手动 Stop。
        Write-Warning "$($Service.Name) owns port $($Service.Port), but its HTTP API is unresponsive; replacing PID $($existing.ProcessId)."
        Stop-Process -Id $existing.ProcessId -Force
        Start-Sleep -Milliseconds 500
    }

    New-Item -ItemType Directory -Force -Path $LogDirectory | Out-Null
    $scriptPath = Join-Path $ProjectRoot $Service.Script
    $stdoutPath = Join-Path $LogDirectory "$($Service.Name).out.log"
    $stderrPath = Join-Path $LogDirectory "$($Service.Name).err.log"
    $processObject = Start-Process `
        -FilePath $PythonExecutable `
        -ArgumentList @("-B", "-u", $scriptPath) `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath `
        -PassThru

    if (-not (Wait-ServiceReady -Service $Service)) {
        $errorTail = if (Test-Path $stderrPath) {
            (Get-Content -LiteralPath $stderrPath -Tail 20 -ErrorAction SilentlyContinue) -join [Environment]::NewLine
        } else {
            "No error log was created."
        }
        Stop-Process -Id $processObject.Id -Force -ErrorAction SilentlyContinue
        throw "$($Service.Name) did not become HTTP healthy on port $($Service.Port).`n$errorTail"
    }
    Write-Host "[OK] Started $($Service.Name) on http://127.0.0.1:$($Service.Port) (PID $($processObject.Id))."
}

function Start-LabServices {
    param(
        $UnifiedRestoreSettings,
        [bool]$RestoreWasChecked = $false
    )

    foreach ($service in @($Services | Sort-Object Port)) {
        Start-LabService -Service $service
    }

    if ($null -ne $UnifiedRestoreSettings) {
        try {
            $json = $UnifiedRestoreSettings | ConvertTo-Json -Depth 8
            Invoke-RestMethod `
                -Method Post `
                -Uri "http://127.0.0.1:8765/api/ai/unified/start" `
                -ContentType "application/json; charset=utf-8" `
                -Body ([System.Text.Encoding]::UTF8.GetBytes($json)) `
                -TimeoutSec 20 | Out-Null
            Write-Host "[OK] Restored the previously running unified AI stream."
        }
        catch {
            Write-Warning "Services are running, but unified stream restore failed: $($_.Exception.Message)"
        }
    }
    elseif ($RestoreWasChecked) {
        Write-Host "[--] Unified AI stream was not healthy/running before restart; it was not auto-started."
    }
}

function Show-LabServiceStatus {
    $rows = foreach ($service in $Services) {
        $info = Get-PortProcessInfo -Service $service
        $healthy = Test-ServiceHealthy -Service $service
        $state = if ($null -ne $info -and -not $info.Expected) {
            "FOREIGN"
        }
        elseif ($healthy) {
            "RUNNING"
        }
        elseif ($null -ne $info) {
            "UNHEALTHY"
        }
        else {
            "STOPPED"
        }

        [pscustomobject]@{
            Service = $service.Name
            Port = $service.Port
            State = $state
            PID = if ($null -eq $info) { "--" } else { $info.ProcessId }
        }
    }
    $rows | Format-Table -AutoSize

    $coreService = $Services | Where-Object { $_.Port -eq 8765 } | Select-Object -First 1
    if (Test-ServiceHealthy -Service $coreService) {
        try {
            $unified = Invoke-RestMethod `
                -Uri "http://127.0.0.1:8765/api/ai/unified/status" `
                -TimeoutSec 5
            Write-Host "Unified AI: running=$($unified.running), frame_id=$($unified.frame_id), error=$($unified.error)"
        }
        catch {
            Write-Warning "Could not query unified AI status: $($_.Exception.Message)"
        }
    }
}

Set-Location $ProjectRoot
Write-Host "USB-6363 service manager: $Action"

switch ($Action) {
    "Start" {
        Start-LabServices -UnifiedRestoreSettings $null -RestoreWasChecked $false
        Show-LabServiceStatus
    }
    "Restart" {
        $restoreSettings = Get-UnifiedRestoreSettings
        Stop-LabServices
        Start-LabServices -UnifiedRestoreSettings $restoreSettings -RestoreWasChecked $true
        Show-LabServiceStatus
    }
    "Stop" {
        Stop-LabServices
        Show-LabServiceStatus
    }
    "Status" {
        Show-LabServiceStatus
    }
}
