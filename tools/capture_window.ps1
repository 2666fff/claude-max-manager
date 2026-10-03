param(
    [Parameter(Mandatory=$true)][long]$WindowHandle,
    [Parameter(Mandatory=$true)][string]$OutputPath
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class DemoWindowCapture {
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left, Top, Right, Bottom; }
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern IntPtr GetAncestor(IntPtr h, uint flags);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr dc, uint flags);
}
'@
[DemoWindowCapture]::SetProcessDPIAware() | Out-Null
$captureHandle = [DemoWindowCapture]::GetAncestor([IntPtr]$WindowHandle, 2)
$captureRect = New-Object DemoWindowCapture+RECT
if (-not [DemoWindowCapture]::GetWindowRect($captureHandle, [ref]$captureRect)) {
    throw 'Unable to locate demo window.'
}
$captureBitmap = New-Object System.Drawing.Bitmap(($captureRect.Right - $captureRect.Left), ($captureRect.Bottom - $captureRect.Top))
$captureGraphics = [System.Drawing.Graphics]::FromImage($captureBitmap)
try {
    $captureDc = $captureGraphics.GetHdc()
    try {
        if (-not [DemoWindowCapture]::PrintWindow($captureHandle, $captureDc, 2)) {
            throw 'Window capture failed.'
        }
    } finally {
        $captureGraphics.ReleaseHdc($captureDc)
    }
    $captureBitmap.Save([System.IO.Path]::GetFullPath($OutputPath), [System.Drawing.Imaging.ImageFormat]::Png)
} finally {
    $captureGraphics.Dispose()
    $captureBitmap.Dispose()
}
