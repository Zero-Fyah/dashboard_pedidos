<#
notificar_fallo_scheduler.ps1 — aviso best-effort de un ciclo del scheduler
con al menos un paso fallido (deuda "notificación de fallo" de CLAUDE.md).

No usa un módulo externo (BurntToast no está instalado) — System.Windows.Forms
ya viene con Windows, y desde Windows 10 un NotifyIcon.ShowBalloonTip() se
renderiza como notificación moderna del Centro de actividades, no como el
globo clásico.

Riesgo conocido, sin verificar en vivo (ver docs/decisions.md): la tarea
programada que llama a este script corre con LogonType=Password ("esté o no
el usuario conectado"), que normalmente significa una sesión sin escritorio
interactivo — si es así, este aviso puede no mostrarse nunca, en silencio.
Confirmar visualmente tras el primer fallo real; si no aparece, la
alternativa es email por SMTP.
#>
param(
    [Parameter(Mandatory = $true)][string]$LogFile
)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$icon = New-Object System.Windows.Forms.NotifyIcon
try {
    $icon.Icon = [System.Drawing.SystemIcons]::Warning
    $icon.Visible = $true
    $icon.BalloonTipTitle = "dashboard_pedidos - fallo en el ciclo del scheduler"
    $icon.BalloonTipText = "Al menos un paso del ciclo fallo. Revisar: $LogFile"
    $icon.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Warning
    $icon.ShowBalloonTip(20000)
    # Sin esta espera, el proceso (y el icono) se destruye antes de que
    # Windows alcance a mostrar/renderizar la notificación.
    Start-Sleep -Seconds 21
} finally {
    $icon.Dispose()
}
