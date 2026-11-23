# run_daily_pipeline_fallback.ps1
#
# Version de run_daily_pipeline.ps1 para cuando la PC esta apagada
# (viajes). Asume que el workflow ya copio el snapshot de source-cache
# (json\_lang\en, etp\, rps\) dentro de $EtpWorkDir ANTES de llamar a este
# script -- por eso los Pasos 1 y 2 (etp.exe all / port-translations) NO
# se ejecutan aca: usamos la ultima copia buena en vez de extraer de
# nuevo del juego. El resto (Pasos 3-8) es identico al script original.
#
# Diferencia clave respecto al original: mientras la PC este apagada,
# el texto JAPONES fuente no se actualiza (se usa el ultimo conocido).
# Lo que SI se actualiza es todo lo que depende de Supabase: traducciones
# ES nuevas que hayan cargado tus colegas, glosario, etc. -- que es
# justamente lo que ellos necesitan para seguir probando mientras viajas.

$ErrorActionPreference = "Stop"

$envFile = Join-Path $PSScriptRoot ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*#' -or $_ -match '^\s*$') { return }
        $key, $value = $_ -split '=', 2
        $key = $key.Trim()
        if (-not (Test-Path "env:$key")) {
            Set-Item -Path "env:$key" -Value $value.Trim()
        }
    }
}

$EtpExe             = $env:ETP_EXE_PATH
$EtpWorkDir         = $env:ETP_WORK_DIR
$LocalDb            = Join-Path $EtpWorkDir "translations.db"
$ClarityDbOutput    = Join-Path $EtpWorkDir "clarity_dbs"
$ChangedReviewCsv   = Join-Path $EtpWorkDir "logs\ja_changed_$(Get-Date -Format yyyy-MM-dd).csv"
$ClarityGlossaryDb  = $env:CLARITY_GLOSSARY_DB_PATH
$ClarityDialogDb    = $env:CLARITY_DIALOG_DB_PATH
$CommonZipOutput    = Join-Path $EtpWorkDir "etp_output\common.zip"
$RawJsonFolder      = Join-Path $EtpWorkDir "json\_lang\en"

if (-not $env:DATABASE_URL) { Write-Error "DATABASE_URL no esta definida."; exit 1 }
if (-not (Test-Path $RawJsonFolder) -or (Get-ChildItem $RawJsonFolder -Filter *.json -Recurse).Count -eq 0) {
    Write-Error "No se encontro el snapshot json_lang_en en $RawJsonFolder. Verifica que el workflow lo haya copiado antes de llamar a este script."
    exit 1
}
Write-Host "Usando snapshot de origen (no se corrio etp.exe all/port-translations)" -ForegroundColor Yellow

Write-Host "`n=== Paso 3: construir snapshot local (build_translation_db.py) ===" -ForegroundColor Cyan
python scripts\build_translation_db.py $RawJsonFolder --output $LocalDb --overwrite
if ($LASTEXITCODE -ne 0) { Write-Error "build_translation_db.py fallo"; exit 1 }

Write-Host "`n=== Paso 4: sincronizar con Supabase (sync_json_updates.py) ===" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path (Split-Path $ChangedReviewCsv) | Out-Null
python scripts\sync_json_updates.py $LocalDb --review-output $ChangedReviewCsv
if ($LASTEXITCODE -ne 0) { Write-Error "sync_json_updates.py fallo"; exit 1 }

Write-Host "`n=== Paso 5: Supabase -> JSON con ES + DBs de Clarity (export_translations.py) ===" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $ClarityDbOutput | Out-Null
$exportArgs = @(
    "--lang", "es", "--all",
    "--output", $RawJsonFolder,
    "--build-clarity-dbs",
    "--clarity-db-output", $ClarityDbOutput
)
python scripts\export_translations.py @exportArgs
if ($LASTEXITCODE -ne 0) { Write-Error "export_translations.py fallo"; exit 1 }

if ($ClarityGlossaryDb -and (Test-Path (Join-Path $ClarityDbOutput "glossary.db"))) {
    Copy-Item (Join-Path $ClarityDbOutput "glossary.db") $ClarityGlossaryDb -Force
}
if ($ClarityDialogDb -and (Test-Path (Join-Path $ClarityDbOutput "clarity_dialog.db"))) {
    Copy-Item (Join-Path $ClarityDbOutput "clarity_dialog.db") $ClarityDialogDb -Force
}

# --- TODO: pendiente de confirmar si esto puede correr en un runner
# hosteado (sin el juego instalado). Ver nota en daily_sync_fallback.yml.
Write-Host "`n=== Paso 6: JSON -> ETP binario (etp.exe rebuild . .) ===" -ForegroundColor Cyan
Push-Location $EtpWorkDir
try {
    & $EtpExe rebuild . .
    if ($LASTEXITCODE -ne 0) { throw "etp.exe rebuild fallo" }
}
finally {
    Pop-Location
}

$CommonFolder = Join-Path $EtpWorkDir "common"
if (-not (Test-Path $CommonFolder)) {
    Write-Error "No se encontro la carpeta common/ tras el rebuild en $CommonFolder"
    exit 1
}

Write-Host "`n=== Paso 7: comprimir common/ -> common.zip ===" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path (Split-Path $CommonZipOutput) | Out-Null
if (Test-Path $CommonZipOutput) { Remove-Item $CommonZipOutput }
Compress-Archive -Path $CommonFolder -DestinationPath $CommonZipOutput

Write-Host "`n=== Paso 8: armar paste_in_dqxclarity.zip ===" -ForegroundColor Cyan
$PasteStaging = Join-Path $EtpWorkDir "etp_output\paste_in_dqxclarity_staging\dqxclarity"
$PasteMiscFiles = Join-Path $PasteStaging "misc_files"
if (Test-Path (Join-Path $EtpWorkDir "etp_output\paste_in_dqxclarity_staging")) {
    Remove-Item (Join-Path $EtpWorkDir "etp_output\paste_in_dqxclarity_staging") -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $PasteMiscFiles | Out-Null
Copy-Item (Join-Path $PSScriptRoot "clarity_patch\main.py") (Join-Path $PasteStaging "main.py") -Force
Copy-Item (Join-Path $PSScriptRoot "clarity_patch\original_main.py") (Join-Path $PasteStaging "original_main.py") -Force
Copy-Item (Join-Path $ClarityDbOutput "glossary.db") (Join-Path $PasteMiscFiles "glossary.db") -Force
Copy-Item (Join-Path $ClarityDbOutput "clarity_dialog.db") (Join-Path $PasteMiscFiles "clarity_dialog.db") -Force

$PasteZipOutput = Join-Path $EtpWorkDir "etp_output\paste_in_dqxclarity.zip"
if (Test-Path $PasteZipOutput) { Remove-Item $PasteZipOutput }
Compress-Archive -Path $PasteStaging -DestinationPath $PasteZipOutput

Copy-Item (Join-Path $PSScriptRoot "readme.md") (Join-Path $EtpWorkDir "etp_output\readme.md") -Force

Write-Host "`n=== Listo (fallback) ===" -ForegroundColor Green
Write-Host "common.zip generado en: $CommonZipOutput"
