# ============================================================
#  注册 Windows 计划任务（astock_ai）
#
#  为什么不用 schtasks 命令行：
#  `schtasks /Create` 无法设置 **StartWhenAvailable**（错过计划时间后尽快补跑）。
#  而本机不保证 24 小时开机 —— 18:30 若关机或休眠，任务会被直接跳过，
#  当天就不会有任何数据与报告。该项只能通过 New-ScheduledTaskSettingsSet 设置。
#
#  注册三个任务：
#    1. AStockAI_Snapshot 每交易日 14:00  盘中快照采集
#       （**不能**并入 18:30 那个：daily 有 data_ready_time=16:00 守卫，
#         14:00 运行会回退到上一交易日，产出错位报告。快照走独立链路。）
#    2. AStockAI_Daily    每交易日 18:30  每日全流程
#       （18:30 晚于 data.data_ready_time=16:00，确保拿到完整日线）
#    3. AStockAI_Backup   每周日  20:00   数据备份
#
#  用法：右键「以管理员身份运行 PowerShell」，然后
#      powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
# ============================================================

$ErrorActionPreference = 'Stop'

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$SnapshotName = 'AStockAI_Snapshot'
$DailyName = 'AStockAI_Daily'
$BackupName = 'AStockAI_Backup'

Write-Host ('=' * 68)
Write-Host '  注册 astock_ai 计划任务'
Write-Host ('  项目目录：' + $Root)
Write-Host ('=' * 68)

function New-AstockTask {
    param(
        [string]$Name,
        [string]$ScriptPath,
        [string]$Description,
        [object]$Trigger
    )

    if (-not (Test-Path $ScriptPath)) {
        throw "脚本不存在：$ScriptPath"
    }

    $action = New-ScheduledTaskAction `
        -Execute 'cmd.exe' `
        -Argument ('/c "{0}"' -f $ScriptPath) `
        -WorkingDirectory $Root

    # StartWhenAvailable 是关键：错过计划时间（关机/休眠）后，开机即补跑。
    # 采集层本身也会自动比对交易日历补齐缺口，两者配合保证不漏数据。
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Hours 3)

    Register-ScheduledTask `
        -TaskName $Name `
        -Action $action `
        -Trigger $Trigger `
        -Settings $settings `
        -Description $Description `
        -Force | Out-Null

    Write-Host ('  ✔ 已注册：' + $Name)
}

try {
    # ---- 1. 盘中快照：交易日 14:00 ----
    # 与 18:30 的每日流程**并存**，不是替代：18:30 跑的是完整盘后链路
    # （复盘/归因/报告/快照导出），14:00 只抓「当天此刻」的截面。
    # 两个任务都能通过 `MultipleInstances IgnoreNew` 防止重入。
    $snapshotTrigger = New-ScheduledTaskTrigger -Weekly `
        -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At '14:00'

    New-AstockTask -Name $SnapshotName `
        -ScriptPath (Join-Path $Root 'scripts\run_snapshot.bat') `
        -Description 'A股AI选股系统：盘中快照采集（14:00，盘中决策的唯一数据来源；历史盘中数据不可补）' `
        -Trigger $snapshotTrigger

    # ---- 2. 每日流程：交易日 18:30 ----
    $dailyTrigger = New-ScheduledTaskTrigger -Weekly `
        -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At '18:30'

    New-AstockTask -Name $DailyName `
        -ScriptPath (Join-Path $Root 'scripts\run_daily.bat') `
        -Description 'A股AI选股系统：盘后 数据补齐→因子→市场状态→选股→AI报告→复盘' `
        -Trigger $dailyTrigger

    # ---- 3. 数据备份：每周日 20:00 ----
    $backupTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At '20:00'

    New-AstockTask -Name $BackupName `
        -ScriptPath (Join-Path $Root 'scripts\backup_data.bat') `
        -Description 'A股AI选股系统：数据备份（主库/Skill库/报告/回测明细）' `
        -Trigger $backupTrigger
}
catch {
    Write-Host ''
    Write-Host ('  ✖ 注册失败：' + $_.Exception.Message)
    Write-Host '    请右键 PowerShell →「以管理员身份运行」后重试。'
    exit 1
}

Write-Host ''
Write-Host ('-' * 68)
Write-Host '  当前状态：'
foreach ($n in @($DailyName, $BackupName)) {
    $info = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
    if ($null -ne $info) {
        $state = $info.State
        Write-Host ('    ' + $n.PadRight(20) + $state)
    }
}

Write-Host ''
Write-Host '  常用操作：'
Write-Host ('    手动跑一次  : schtasks /Run /TN "' + $DailyName + '"')
Write-Host ('    查看下次时间: schtasks /Query /TN "' + $DailyName + '" /V /FO LIST | findstr "下次"')
Write-Host ('    删除任务    : schtasks /Delete /TN "' + $DailyName + '" /F')
Write-Host ('    删除备份任务: schtasks /Delete /TN "' + $BackupName + '" /F')
Write-Host ''
Write-Host '  说明：任务错过计划时间（关机/休眠）后，会在机器可用时自动补跑；'
Write-Host '        采集层还会比对交易日历补齐缺失日，因此不会漏数据。'
Write-Host ('=' * 68)
