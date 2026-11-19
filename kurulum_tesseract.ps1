# Tesseract OCR'i yonetici hakki gerektirmeden kullanici klasorune kurar.
# Kullanim: powershell -ExecutionPolicy Bypass -File .\kurulum_tesseract.ps1
param(
    [string]$Yol = "$env:LOCALAPPDATA\Programs\Tesseract-OCR"
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$exe = Join-Path $Yol "tesseract.exe"
$dil = Join-Path $Yol "tessdata\eng.traineddata"

# NSIS /D parametresini yok sayarsa kurulum varsayilan konuma gider.
# Bu yuzden hem istenen hem standart yollar kontrol edilir.
function Find-Tesseract {
    param([string]$Exe, [string]$Dil)
    if ((Test-Path $Exe) -and (Test-Path $Dil)) { return $Exe }
    $adaylar = @(
        (Join-Path $env:ProgramFiles "Tesseract-OCR\tesseract.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Tesseract-OCR\tesseract.exe")
    )
    foreach ($aday in $adaylar) {
        if ($aday -and (Test-Path $aday)) { return $aday }
    }
    return $null
}

$bulunan = Find-Tesseract -Exe $exe -Dil $dil
if ($bulunan) {
    $exe = $bulunan
    $dil = Join-Path (Split-Path $exe -Parent) "tessdata\eng.traineddata"
    Write-Host "Tesseract zaten kurulu: $exe"
} else {
    $url = "https://github.com/UB-Mannheim/tesseract/releases/download/v5.4.0.20240606/tesseract-ocr-w64-setup-5.4.0.20240606.exe"
    $setup = Join-Path $env:TEMP "tesseract-setup.exe"
    Write-Host "Indiriliyor: $url"
    Invoke-WebRequest -Uri $url -OutFile $setup -UseBasicParsing

    Write-Host "Kuruluyor: $Yol"
    # NSIS: /S sessiz kurulum, /D hedef klasor (son parametre, tirnaksiz).
    $komut = '"{0}" /S /D={1}' -f $setup, $Yol
    $proc = Start-Process -FilePath $env:ComSpec -ArgumentList "/c", $komut -Wait -PassThru
    if ($proc.ExitCode -ne 0) {
        throw "Kurulum basarisiz (cikis kodu $($proc.ExitCode))."
    }
    Remove-Item $setup -Force -ErrorAction SilentlyContinue

    $bulunan = Find-Tesseract -Exe $exe -Dil $dil
    if ($bulunan) {
        $exe = $bulunan
        $dil = Join-Path (Split-Path $exe -Parent) "tessdata\eng.traineddata"
        if ($exe -ne (Join-Path $Yol "tesseract.exe")) {
            Write-Host "Kurulum varsayilan konuma yapildi: $exe"
        }
    }
}

if (-not (Test-Path $exe)) { throw "tesseract.exe bulunamadi: $exe" }
if (-not (Test-Path $dil)) { throw "eng dil verisi bulunamadi: $dil" }

# Uygulama bu degiskeni okuyor; kalici olarak kullanici ortamina yazilir.
[Environment]::SetEnvironmentVariable("TESSERACT_CMD", $exe, "User")
$env:TESSERACT_CMD = $exe

Write-Host ""
Write-Host "Tesseract hazir: $exe"
& $exe --version | Select-Object -First 1
& $exe --list-langs
Write-Host ""
Write-Host "Yeni bir terminal acin veya uygulamayi yeniden baslatin."
