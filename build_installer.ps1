# Builds dist-installer\GPU-Screen-Recorder-Setup.exe (runs build.ps1 first unless -SkipApp)
param([switch]$SkipApp)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not $SkipApp) { & powershell -NoProfile -ExecutionPolicy Bypass -File build.ps1; if ($LASTEXITCODE) { throw "app build failed" } }

$stage = "installer\_stage"
Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force "$stage\showcase", "$stage\fonts" | Out-Null
Compress-Archive -Path "dist\GPU Screen Recorder" -DestinationPath "$stage\payload.zip" -CompressionLevel Optimal
Copy-Item gsr\images\gpu_screen_recorder_logo.png "$stage\logo.png"
Copy-Item gsr\fonts\NotoSans-*.ttf "$stage\fonts\"
foreach ($n in "01-overlay", "02-dropdown", "03-replay-settings", "05-global-settings", "06-gallery", "07-player") {
    Copy-Item "docs\media\$n.png" "$stage\showcase\"
}
python -c "from PIL import Image, ImageDraw; im=Image.new('RGBA',(30,30),(0,0,0,0)); d=ImageDraw.Draw(im); d.line([(6,15),(12,21),(24,8)], fill=(255,255,255,255), width=4); im.save(r'$stage\check.png')"

python -m PyInstaller --noconfirm --clean --onefile --windowed --name "GPU-Screen-Recorder-Setup" --icon gsr\icon.ico `
    --distpath dist-installer --workpath build-installer `
    --add-data "$stage\payload.zip;." --add-data "$stage\logo.png;." --add-data "$stage\check.png;." `
    --add-data "$stage\showcase;showcase" --add-data "$stage\fonts;fonts" `
    --exclude-module tkinter --exclude-module PySide6.QtMultimedia --exclude-module PySide6.QtNetwork `
    installer\installer.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
Remove-Item -Recurse -Force build-installer, "GPU-Screen-Recorder-Setup.spec", $stage -ErrorAction SilentlyContinue
"Built: $PSScriptRoot\dist-installer\GPU-Screen-Recorder-Setup.exe"
