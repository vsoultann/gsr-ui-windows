# Builds dist\GPU Screen Recorder\GPU Screen Recorder.exe (with ffmpeg.exe next to it)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python -c "from PIL import Image; Image.open('gsr/images/gpu_screen_recorder_logo.png').save('gsr/icon.ico', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128)])"

python -m PyInstaller --noconfirm --clean --windowed --name "GPU Screen Recorder" --icon gsr\icon.ico `
    --add-data "gsr\images;gsr\images" --add-data "gsr\translations;gsr\translations" --add-data "gsr\fonts;gsr\fonts" `
    --exclude-module tkinter --exclude-module customtkinter `
    main.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$ffmpeg = (python -c "from gsr import system; print(system.find_ffmpeg())").Trim()
Copy-Item $ffmpeg "dist\GPU Screen Recorder\ffmpeg.exe" -Force
Copy-Item LICENSE "dist\GPU Screen Recorder\LICENSE.txt" -Force
"Built: $PSScriptRoot\dist\GPU Screen Recorder\GPU Screen Recorder.exe"
