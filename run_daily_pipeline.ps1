# run_daily_pipeline.ps1
#
# End-to-end DQX ES pipeline, from the game's raw DAT files all the way to
# a common.zip ready for Clarity's .clpk packaging tool. Collapses what
# used to be 9 manual steps into one script call -- only the final
# common.zip -> .clpk conversion (Clarity's own tool) stays manual, since
# we don't yet know if that step has a CLI equivalent.
#
#   1. etp.exe all .                -> DAT -> raw JSON (ETPLocalizer, deja
#                                      common/, etp/, json/, rps/ en la
#                                      misma carpeta donde corres etp.exe)
#   2. etp.exe port-translations .  -> merges in Clarity's current EN
#   3. build_translation_db.py      -> JSON -> local SQLite snapshot
#      (fuente real confirmada: json\_lang\en)
#   4. sync_json_updates.py         -> SQLite -> Supabase
#   5. export_translations.py       -> Supabase -> ES JSON, escrito
#                                      DIRECTAMENTE en json\_lang\en
#                                      (confirmado como la ubicacion real
#                                      que etp.exe rebuild lee) + fresh
#                                      glossary.db / clarity_dialog.db en
#                                      esa misma carpeta
#   6. etp.exe rebuild . .          -> JSON -> ETP binaries in common/
#   7. Compress common/ -> common.zip
#
# This script is meant to be run the same way whether you trigger it by
# hand or via a scheduled GitHub Actions self-hosted runner -- all the
# logic lives here, not duplicated in the workflow YAML.
#
# Requires: DATABASE_URL environment variable set to your Supabase
# connection string (pooler, not direct -- see earlier notes on IPv6).

$ErrorActionPreference = "Stop"

# --- Cargar variables desde .env si existe (no sobrescribe variables ya definidas en la sesion) ---
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
    Write-Host "Variables cargadas desde .env" -ForegroundColor DarkGray
}

# --- Rutas con Join-Path para evitar colisiones ---
$EtpExe             = $env:ETP_EXE_PATH
$EtpWorkDir         = $env:ETP_WORK_DIR
$LocalDb            = Join-Path $EtpWorkDir "translations.db"
$ClarityDbOutput    = Join-Path $EtpWorkDir "clarity_dbs"   # separado de json\_lang\en a proposito
$ChangedReviewCsv   = Join-Path $EtpWorkDir "logs\ja_changed_$(Get-Date -Format yyyy-MM-dd).csv"
$BackupCsv          = Join-Path $EtpWorkDir "backups\backup_$(Get-Date -Format yyyy-MM-dd).csv"
$ClarityGlossaryDb  = $env:CLARITY_GLOSSARY_DB_PATH
$ClarityDialogDb    = $env:CLARITY_DIALOG_DB_PATH
$CommonZipOutput    = Join-Path $EtpWorkDir "etp_output\common.zip"

if (-not $env:DATABASE_URL) {
    Write-Error "DATABASE_URL no esta definida. Configurala antes de correr este script."
    exit 1
}
if (-not $EtpExe -or -not (Test-Path $EtpExe)) {
    Write-Error "ETP_EXE_PATH no apunta a un etp.exe valido. Definelo en tu .env."
    exit 1
}
if (-not $EtpWorkDir -or -not (Test-Path $EtpWorkDir)) {
    Write-Error "ETP_WORK_DIR no apunta a una carpeta valida. Definelo en tu .env."
    exit 1
}

# Limpiar restos de corridas anteriores ANTES de extraer/reconstruir --
# common/, etp/, rps/ no se limpian solas entre corridas, asi que sin esto
# es posible que un common.zip termine con una mezcla de RPS nuevos y
# viejos (de antes de un fix, por ejemplo). Preferimos una corrida un poco
# mas lenta pero garantizadamente limpia.
foreach ($folder in @("common", "etp", "rps")) {
    $path = Join-Path $EtpWorkDir $folder
    if (Test-Path $path) {
        Write-Host "Limpiando $path de una corrida anterior..." -ForegroundColor DarkGray
        Remove-Item $path -Recurse -Force
    }
}

Push-Location $EtpWorkDir
try {
    Write-Host "=== Paso 1: DAT -> JSON crudo (etp.exe all .) ===" -ForegroundColor Cyan
    & $EtpExe all .
    if ($LASTEXITCODE -ne 0) { throw "etp.exe all fallo" }

    Write-Host "`n=== Paso 2: importar EN de Clarity (etp.exe port-translations .) ===" -ForegroundColor Cyan
    & $EtpExe port-translations .
    if ($LASTEXITCODE -ne 0) { throw "etp.exe port-translations fallo" }
}
finally {
    Pop-Location
}

$RawJsonFolder = Join-Path $EtpWorkDir "json\_lang\en"
if (-not (Test-Path $RawJsonFolder) -or (Get-ChildItem $RawJsonFolder -Filter *.json -Recurse).Count -eq 0) {
    Write-Error "No se encontraron JSON en $RawJsonFolder tras correr ETPLocalizer."
    exit 1
}
$jsonFileCount = (Get-ChildItem $RawJsonFolder -Filter *.json -Recurse).Count
Write-Host "OK: $jsonFileCount JSON de origen en $RawJsonFolder"

# --- Snapshot para runs de respaldo (fallback) ---
# En este punto json\_lang\en todavia tiene el EN recien extraido/mergeado
# (Paso 5 lo va a sobreescribir con ES mas adelante), y etp\ + rps\ ya
# tienen la estructura que "etp.exe rebuild" necesita como base. Guardamos
# una copia de estos tres antes de que se pierdan, para que un run sin PC
# encendida (sin etp.exe / sin el juego) pueda saltarse los Pasos 1-2 y
# reusar esto como "la ultima copia buena".
Write-Host "`n=== Guardando snapshot de origen para runs de respaldo (source_cache) ===" -ForegroundColor Cyan
$SourceCacheDir = Join-Path $EtpWorkDir "source_cache"
if (Test-Path $SourceCacheDir) { Remove-Item $SourceCacheDir -Recurse -Force }
New-Item -ItemType Directory -Force -Path $SourceCacheDir | Out-Null
Copy-Item $RawJsonFolder (Join-Path $SourceCacheDir "json_lang_en") -Recurse
Copy-Item (Join-Path $EtpWorkDir "etp") (Join-Path $SourceCacheDir "etp") -Recurse
Copy-Item (Join-Path $EtpWorkDir "rps") (Join-Path $SourceCacheDir "rps") -Recurse
Write-Host "Snapshot guardado en $SourceCacheDir" -ForegroundColor Green

Write-Host "`n=== Paso 3: construir snapshot local (build_translation_db.py) ===" -ForegroundColor Cyan
python scripts\build_translation_db.py $RawJsonFolder --output $LocalDb --overwrite
if ($LASTEXITCODE -ne 0) { Write-Error "build_translation_db.py fallo"; exit 1 }

Write-Host "`n=== Paso 4: sincronizar con Supabase (sync_json_updates.py) ===" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path (Split-Path $ChangedReviewCsv) | Out-Null
python scripts\sync_json_updates.py $LocalDb --review-output $ChangedReviewCsv
if ($LASTEXITCODE -ne 0) { Write-Error "sync_json_updates.py fallo"; exit 1 }

if (Test-Path $ChangedReviewCsv) {
    Write-Host "`n[!] Hay entradas con JA modificado que requieren revision manual: $ChangedReviewCsv" -ForegroundColor Yellow
}

# --- Versión Experimental (Caracteres normales) ---
Write-Host "`n=== Paso 5.1 (Experimental): Supabase -> JSON normal + DBs de Clarity ===" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $ClarityDbOutput | Out-Null
$exportArgsExp = @(
    "--lang", "es", "--all",
    "--output", $RawJsonFolder,
    "--build-clarity-dbs",
    "--clarity-db-output", $ClarityDbOutput
)
python scripts\export_translations.py @exportArgsExp
if ($LASTEXITCODE -ne 0) { Write-Error "export_translations.py (experimental) fallo"; exit 1 }

# Copiamos bases de datos de Clarity si están configuradas
$generatedGlossaryDb = Join-Path $ClarityDbOutput "glossary.db"
$generatedDialogDb   = Join-Path $ClarityDbOutput "clarity_dialog.db"
if ($ClarityGlossaryDb -and (Test-Path $generatedGlossaryDb)) {
    Copy-Item $generatedGlossaryDb $ClarityGlossaryDb -Force
    Write-Host "glossary.db copiada a $ClarityGlossaryDb"
}
if ($ClarityDialogDb -and (Test-Path $generatedDialogDb)) {
    Copy-Item $generatedDialogDb $ClarityDialogDb -Force
    Write-Host "clarity_dialog.db copiada a $ClarityDialogDb"
}

Write-Host "`n=== Paso 6.1 (Experimental): etp.exe rebuild (experimental) ===" -ForegroundColor Cyan
Push-Location $EtpWorkDir
try {
    & $EtpExe rebuild . .
    if ($LASTEXITCODE -ne 0) { throw "etp.exe rebuild experimental fallo" }
}
finally {
    Pop-Location
}

$CommonFolder = Join-Path $EtpWorkDir "common"
if (-not (Test-Path $CommonFolder)) {
    Write-Error "No se encontro la carpeta common/ tras el rebuild experimental en $CommonFolder"
    exit 1
}

# Respaldar temporalmente la carpeta common experimental antes de sobrescribirla
$ExpStagingCommon = Join-Path $EtpWorkDir "common_exp_staging"
if (Test-Path $ExpStagingCommon) { Remove-Item $ExpStagingCommon -Recurse -Force }
Copy-Item $CommonFolder $ExpStagingCommon -Recurse


# --- Versión Vanilla / Segura (Con limpieza de encoding / REPLACE_MAP) ---
Write-Host "`n=== Paso 5.2 (Vanilla): Supabase -> JSON con limpieza de encoding ===" -ForegroundColor Cyan
$exportArgsVanilla = @(
    "--lang", "es", "--all",
    "--output", $RawJsonFolder,
    "--clean-encoding"
)
python scripts\export_translations.py @exportArgsVanilla
if ($LASTEXITCODE -ne 0) { Write-Error "export_translations.py (vanilla) fallo"; exit 1 }

Write-Host "`n=== Paso 6.2 (Vanilla): etp.exe rebuild (vanilla) ===" -ForegroundColor Cyan
Push-Location $EtpWorkDir
try {
    & $EtpExe rebuild . .
    if ($LASTEXITCODE -ne 0) { throw "etp.exe rebuild vanilla fallo" }
}
finally {
    Pop-Location
}

# Verificacion de frescura para common vanilla
$staleThreshold = (Get-Date).AddMinutes(-10)
$staleFiles = Get-ChildItem $CommonFolder -Recurse -File | Where-Object { $_.LastWriteTime -lt $staleThreshold }
if ($staleFiles) {
    Write-Warning "$($staleFiles.Count) archivo(s) en common/ (vanilla) NO se modificaron en esta corrida."
} else {
    Write-Host "OK: todos los archivos en common/ (vanilla) son de esta corrida." -ForegroundColor Green
}


# --- Paso 7: Comprimir paquetes finales (Vanilla y Experimental separados) ---
Write-Host "`n=== Paso 7: Comprimir carpetas en zips independientes ===" -ForegroundColor Cyan
$EtpOutputFolder = Join-Path $EtpWorkDir "etp_output"
New-Item -ItemType Directory -Force -Path $EtpOutputFolder | Out-Null

$CommonZipOutput = Join-Path $EtpOutputFolder "common.zip"          # Exclusivamente Vanilla (sin plugins)
$CommonExpZipOutput = Join-Path $EtpOutputFolder "common_exp.zip"    # Experimental (con plugins)

if (Test-Path $CommonZipOutput) { Remove-Item $CommonZipOutput -Force }
if (Test-Path $CommonExpZipOutput) { Remove-Item $CommonExpZipOutput -Force }

# 1. Zip Vanilla: SOLO la carpeta common actual (que ya fue construida con --clean-encoding y SIN plugins)
Compress-Archive -Path "$CommonFolder\*" -DestinationPath $CommonZipOutput -Force
Write-Host "common.zip generado (Vanilla: limpio de caracteres y sin plugins)" -ForegroundColor Green

# 2. Zip Experimental: Carpeta common experimental (con tildes/eñes intactas) + carpeta plugins
$PluginsFolder = Join-Path $EtpWorkDir "plugins"
$ExpFinalStaging = Join-Path $EtpOutputFolder "exp_final_staging"
if (Test-Path $ExpFinalStaging) { Remove-Item $ExpFinalStaging -Recurse -Force }

New-Item -ItemType Directory -Force -Path (Join-Path $ExpFinalStaging "common") | Out-Null
Copy-Item "$ExpStagingCommon\*" (Join-Path $ExpFinalStaging "common") -Recurse -Force

if (Test-Path $PluginsFolder) {
    New-Item -ItemType Directory -Force -Path (Join-Path $ExpFinalStaging "plugins") | Out-Null
    Copy-Item "$PluginsFolder\*" (Join-Path $ExpFinalStaging "plugins") -Recurse -Force
    Write-Host "Carpeta plugins integrada al paquete experimental." -ForegroundColor DarkGray
} else {
    Write-Warning "Carpeta plugins no encontrada en $PluginsFolder"
}

Compress-Archive -Path "$ExpFinalStaging\*" -DestinationPath $CommonExpZipOutput -Force
Remove-Item $ExpStagingCommon -Recurse -Force
Remove-Item $ExpFinalStaging -Recurse -Force
Write-Host "common_exp.zip generado (Experimental: caracteres normales + plugins)" -ForegroundColor Green

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
Write-Host "paste_in_dqxclarity.zip generado en: $PasteZipOutput" -ForegroundColor Green

# Copiamos el readme del repo (estatico, lo editas tu directamente en el
# repositorio) junto al resto de los entregables de esta corrida.
Copy-Item (Join-Path $PSScriptRoot "readme.md") (Join-Path $EtpWorkDir "etp_output\readme.md") -Force

Write-Host "`n=== Paso 9: comprimir source_cache (snapshot para fallback) ===" -ForegroundColor Cyan
$SourceCacheZip = Join-Path $EtpWorkDir "source_cache.zip"
if (Test-Path $SourceCacheZip) { Remove-Item $SourceCacheZip }
Compress-Archive -Path (Join-Path $SourceCacheDir "*") -DestinationPath $SourceCacheZip
Write-Host "source_cache.zip generado en: $SourceCacheZip" -ForegroundColor Green

Write-Host "`n=== Listo ===" -ForegroundColor Green
Write-Host "common.zip generado en: $CommonZipOutput"
Write-Host "espanol.clpk generado en: $EspanolClpkOutput"
