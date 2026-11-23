# run_daily_pipeline.ps1
#
# Pipeline DQX ES completo: de los DAT del juego a dos paquetes .clpk listos
# (vanilla y experimental), que conviven con dqxclarity.
#
#   1. etp.exe all .                -> DAT -> JSON crudo
#   2. etp.exe port-translations .  -> mezcla el EN actual de Clarity
#   2.5 fetch_clarity_sources.py    -> custom_*.json de dqx-custom-translations,
#                                      ignore.py y merge.xlsx (Google Sheet), con
#                                      copia de la ultima version buena
#   3. build_translation_db.py      -> JSON del juego + custom_* -> SQLite local
#   4. sync_json_updates.py         -> SQLite -> Supabase
#   5. export_translations.py       -> Supabase -> ES JSON (json\_lang\en) y, por
#                                      variante, un pack dqxclarity\ (6 nombres,
#                                      custom_*, glossary.csv, merge.xlsx)
#   6. etp.exe rebuild . .          -> JSON -> ETP binarios en common\
#   7. Dos zips con la misma logica de estructura:
#        common.zip     (Vanilla)      -> common + dqxclarity           (--clean-encoding)
#        common_exp.zip (Experimental) -> common + plugins + dqxclarity (sin limpiar)
#      dqxclarity\ NO se extrae: dqxclarity lo lee directamente del .clpk.
#
# -FromSnapshot: modo de respaldo (PC apagada). Se salta los Pasos 1-2 y usa el
# snapshot (json\_lang\en, etp\, rps\, plugins\, fetch_cache\) que el workflow
# dejo en ETP_WORK_DIR. run_daily_pipeline_fallback.ps1 solo llama a este script.
#
# Requiere: DATABASE_URL (pooler de Supabase). Opcional: MERGE_SHEET_URL (Google
# Sheet con el merge, compartido "cualquiera con el enlace puede ver").
param([switch]$FromSnapshot)

$ErrorActionPreference = "Stop"

# --- Helpers de empaquetado ---
# Compress-Archive de Windows PowerShell 5.1 (el que usa "shell: powershell" en el
# workflow) guarda las rutas internas con "\" en vez de "/", y segun como se arme
# el -Path la estructura cambia ("carpeta\*" mete el contenido en la raiz, "carpeta"
# mete la carpeta). Para que ambos zips salgan IDENTICOS en estructura y con "/"
# estandar, los armamos explicitamente: cada carpeta origen va bajo un nombre fijo
# en la raiz del zip.
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

function New-ZipFromFolders {
    param(
        [Parameter(Mandatory)][string]$DestinationPath,
        [Parameter(Mandatory)][System.Collections.IDictionary]$Folders   # nombre_en_zip -> carpeta origen
    )
    if (Test-Path $DestinationPath) { Remove-Item $DestinationPath -Force }
    $zip = [System.IO.Compression.ZipFile]::Open($DestinationPath, [System.IO.Compression.ZipArchiveMode]::Create)
    try {
        foreach ($rootName in $Folders.Keys) {
            $srcFolder = (Resolve-Path -LiteralPath $Folders[$rootName]).Path.TrimEnd('\')
            foreach ($file in (Get-ChildItem -LiteralPath $srcFolder -Recurse -File -Force)) {
                $relative  = $file.FullName.Substring($srcFolder.Length + 1).Replace('\', '/')
                $entryName = "$rootName/$relative"
                [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                    $zip, $file.FullName, $entryName, [System.IO.Compression.CompressionLevel]::Optimal)
            }
        }
    }
    finally {
        $zip.Dispose()
    }
}

function Get-ZipTopLevelNames {
    param([Parameter(Mandatory)][string]$ZipPath)
    $z = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        return @($z.Entries | ForEach-Object { ($_.FullName -split '/')[0] } | Sort-Object -Unique)
    }
    finally {
        $z.Dispose()
    }
}

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
$ChangedReviewCsv   = Join-Path $EtpWorkDir "logs\ja_changed_$(Get-Date -Format yyyy-MM-dd).csv"
$BackupCsv          = Join-Path $EtpWorkDir "backups\backup_$(Get-Date -Format yyyy-MM-dd).csv"
$SheetUrl           = $env:MERGE_SHEET_URL
$FetchOut           = Join-Path $EtpWorkDir "clarity_sources"
$FetchCache         = Join-Path $EtpWorkDir "fetch_cache"
$PackExp            = Join-Path $EtpWorkDir "pack_exp"
$PackVanilla        = Join-Path $EtpWorkDir "pack_vanilla"

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

# Plugin del paquete experimental: debe llamarse EXACTAMENTE "plugins" dentro del zip.
$PluginFolder = $null
$pluginCandidates = @(
    (Join-Path $EtpWorkDir "plugins"),
    (Join-Path $EtpWorkDir "plugin"),
    (Join-Path $PSScriptRoot "plugins"),
    (Join-Path $PSScriptRoot "plugin")
)
foreach ($candidate in $pluginCandidates) {
    if (Test-Path $candidate -PathType Container) { $PluginFolder = $candidate; break }
}
if (-not $PluginFolder) {
    throw "No se encontro la carpeta plugins para el paquete experimental. Busque en:`n  " + ($pluginCandidates -join "`n  ")
}
Write-Host "Carpeta plugins: $PluginFolder" -ForegroundColor DarkGray

if (-not $FromSnapshot) {
    # Limpiar restos de corridas anteriores ANTES de extraer/reconstruir.
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
}
else {
    Write-Host "Modo -FromSnapshot: sin etp.exe all / port-translations (se usa el snapshot)" -ForegroundColor Yellow
}

$RawJsonFolder = Join-Path $EtpWorkDir "json\_lang\en"
if (-not (Test-Path $RawJsonFolder) -or (Get-ChildItem $RawJsonFolder -Filter *.json -Recurse).Count -eq 0) {
    Write-Error "No se encontraron JSON en $RawJsonFolder tras correr ETPLocalizer."
    exit 1
}
$jsonFileCount = (Get-ChildItem $RawJsonFolder -Filter *.json -Recurse).Count
Write-Host "OK: $jsonFileCount JSON de origen en $RawJsonFolder"

Write-Host "`n=== Paso 2.5: fuentes de dqxclarity (fetch_clarity_sources.py) ===" -ForegroundColor Cyan
$fetchArgs = @("--out-dir", $FetchOut, "--cache-dir", $FetchCache)
if ($SheetUrl) { $fetchArgs += @("--sheet-url", $SheetUrl) }
python scripts\fetch_clarity_sources.py @fetchArgs
if ($LASTEXITCODE -ne 0) { Write-Error "fetch_clarity_sources.py fallo"; exit 1 }
$CustomJsonFolder = Join-Path $FetchOut "custom_json"
$MergeXlsx        = Join-Path $FetchOut "merge.xlsx"
$IgnoreFile       = Join-Path $FetchOut "ignore.py"
if (-not (Get-ChildItem $CustomJsonFolder -Filter custom_*.json -ErrorAction SilentlyContinue)) {
    Write-Host "[!] Sin custom_*.json de upstream: el pack saldra solo con los 6 nombres + lo que ya este en Supabase." -ForegroundColor Yellow
}
if (-not (Test-Path $MergeXlsx)) {
    Write-Host "[!] Sin merge.xlsx: el pack llevara una plantilla vacia." -ForegroundColor Yellow
}

# --- Snapshot para runs de respaldo (fallback) ---
# json\_lang\en todavia tiene el EN recien extraido (el Paso 5 lo sobreescribe con ES),
# y etp\ + rps\ son la base de "etp.exe rebuild". Se guardan tambien plugins\ y
# fetch_cache\ (ultimas fuentes buenas) para que un run sin PC arme los mismos paquetes.
if (-not $FromSnapshot) {
    Write-Host "`n=== Guardando snapshot de origen para runs de respaldo (source_cache) ===" -ForegroundColor Cyan
    $SourceCacheDir = Join-Path $EtpWorkDir "source_cache"
    if (Test-Path $SourceCacheDir) { Remove-Item $SourceCacheDir -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $SourceCacheDir | Out-Null
    Copy-Item $RawJsonFolder (Join-Path $SourceCacheDir "json_lang_en") -Recurse
    Copy-Item (Join-Path $EtpWorkDir "etp") (Join-Path $SourceCacheDir "etp") -Recurse
    Copy-Item (Join-Path $EtpWorkDir "rps") (Join-Path $SourceCacheDir "rps") -Recurse
    Copy-Item $PluginFolder (Join-Path $SourceCacheDir "plugins") -Recurse
    if (Test-Path $FetchCache) { Copy-Item $FetchCache (Join-Path $SourceCacheDir "fetch_cache") -Recurse }
    Write-Host "Snapshot guardado en $SourceCacheDir" -ForegroundColor Green
}

Write-Host "`n=== Paso 3: construir snapshot local (build_translation_db.py) ===" -ForegroundColor Cyan
$dbInputs = @($RawJsonFolder)
if (Get-ChildItem $CustomJsonFolder -Filter custom_*.json -ErrorAction SilentlyContinue) { $dbInputs += $CustomJsonFolder }
python scripts\build_translation_db.py @dbInputs --output $LocalDb --overwrite
if ($LASTEXITCODE -ne 0) { Write-Error "build_translation_db.py fallo"; exit 1 }

Write-Host "`n=== Paso 4: sincronizar con Supabase (sync_json_updates.py) ===" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path (Split-Path $ChangedReviewCsv) | Out-Null
python scripts\sync_json_updates.py $LocalDb --review-output $ChangedReviewCsv
if ($LASTEXITCODE -ne 0) { Write-Error "sync_json_updates.py fallo"; exit 1 }

if (Test-Path $ChangedReviewCsv) {
    Write-Host "`n[!] Hay entradas con JA modificado que requieren revision manual: $ChangedReviewCsv" -ForegroundColor Yellow
}

# Argumentos comunes del pack dqxclarity (merge/ignore/overrides solo si existen)
$packExtra = @()
if (Test-Path $MergeXlsx)  { $packExtra += @("--merge-xlsx", $MergeXlsx) }
if (Test-Path $IgnoreFile) { $packExtra += @("--ignore-file", $IgnoreFile) }
$OverridesFile = Join-Path $PSScriptRoot "scripts\glossary_overrides.csv"
if (Test-Path $OverridesFile) { $packExtra += @("--glossary-overrides", $OverridesFile) }

# --- Version Experimental (caracteres normales) ---
Write-Host "`n=== Paso 5.1 (Experimental): Supabase -> JSON normal + pack dqxclarity ===" -ForegroundColor Cyan
$exportArgsExp = @(
    "--lang", "es", "--all",
    "--output", $RawJsonFolder,
    "--clarity-pack-dir", $PackExp
) + $packExtra
python scripts\export_translations.py @exportArgsExp
if ($LASTEXITCODE -ne 0) { Write-Error "export_translations.py (experimental) fallo"; exit 1 }

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
Write-Host "`n=== Paso 5.2 (Vanilla): Supabase -> JSON con limpieza de encoding + pack dqxclarity ===" -ForegroundColor Cyan
$exportArgsVanilla = @(
    "--lang", "es", "--all",
    "--output", $RawJsonFolder,
    "--clean-encoding",
    "--clarity-pack-dir", $PackVanilla
) + $packExtra
python scripts\export_translations.py @exportArgsVanilla
if ($LASTEXITCODE -ne 0) { Write-Error "export_translations.py (vanilla) fallo"; exit 1 }

Write-Host "`n=== Paso 6.2 (Vanilla): etp.exe rebuild (vanilla) ===" -ForegroundColor Cyan
$vanillaRebuildStart = Get-Date
Push-Location $EtpWorkDir
try {
    & $EtpExe rebuild . .
    if ($LASTEXITCODE -ne 0) { throw "etp.exe rebuild vanilla fallo" }
}
finally {
    Pop-Location
}

# Verificacion 1: archivos de common/ que el rebuild vanilla NO toco (pueden ser
# restos del rebuild experimental). Se compara contra el INICIO de este rebuild
# (antes se comparaba contra "hace 10 min", lo que dejaba pasar los archivos del
# rebuild experimental como si fueran frescos).
$staleFiles = Get-ChildItem $CommonFolder -Recurse -File | Where-Object { $_.LastWriteTime -lt $vanillaRebuildStart }
if ($staleFiles) {
    Write-Host "Aviso: $($staleFiles.Count) archivo(s) en common/ no fueron reescritos por el rebuild vanilla (normal si su texto no cambia con clean-encoding)." -ForegroundColor DarkGray
}

# Verificacion 2 (la importante): vanilla y experimental DEBEN diferir en al menos un
# archivo (tildes/enes limpiadas). Si son identicos, el rebuild vanilla no sobreescribio
# nada y common.zip saldria con el texto experimental.
$vanillaDiffersFromExp = $false
foreach ($vf in (Get-ChildItem $CommonFolder -Recurse -File)) {
    $relPath = $vf.FullName.Substring($CommonFolder.Length).TrimStart('\')
    $ef = Join-Path $ExpStagingCommon $relPath
    if (-not (Test-Path $ef)) { $vanillaDiffersFromExp = $true; break }
    if ((Get-Item $ef).Length -ne $vf.Length) { $vanillaDiffersFromExp = $true; break }
    if ((Get-FileHash $vf.FullName -Algorithm MD5).Hash -ne (Get-FileHash $ef -Algorithm MD5).Hash) { $vanillaDiffersFromExp = $true; break }
}
if (-not $vanillaDiffersFromExp) {
    throw "common/ (vanilla) es identico a common/ (experimental): el rebuild vanilla no aplico clean-encoding. Abortando para no publicar zips duplicados."
}
Write-Host "OK: common/ (vanilla) difiere de la version experimental, como corresponde." -ForegroundColor Green


# --- Paso 7: Comprimir paquetes finales (Vanilla y Experimental separados) ---
Write-Host "`n=== Paso 7: Comprimir carpetas en zips independientes ===" -ForegroundColor Cyan
$EtpOutputFolder = Join-Path $EtpWorkDir "etp_output"
New-Item -ItemType Directory -Force -Path $EtpOutputFolder | Out-Null

$CommonZipOutput    = Join-Path $EtpOutputFolder "common.zip"      # Vanilla: common/ limpio, SIN plugin
$CommonExpZipOutput = Join-Path $EtpOutputFolder "common_exp.zip"  # Experimental: common/ sin limpiar + plugin

foreach ($p in @($PackExp, $PackVanilla)) {
    if (-not (Test-Path (Join-Path $p "dqxclarity\custom-translations\csv\glossary.csv"))) {
        throw "El pack $p no tiene dqxclarity\custom-translations\csv\glossary.csv"
    }
}

# 1. Vanilla: common/ (clean-encoding) + dqxclarity/ (vanilla). Sin plugin.
New-ZipFromFolders -DestinationPath $CommonZipOutput -Folders ([ordered]@{
    "common"     = $CommonFolder
    "dqxclarity" = (Join-Path $PackVanilla "dqxclarity")
})

# 2. Experimental: common/ (sin limpiar, del staging) + plugins/ + dqxclarity/ (experimental)
New-ZipFromFolders -DestinationPath $CommonExpZipOutput -Folders ([ordered]@{
    "common"     = $ExpStagingCommon
    "plugins"    = $PluginFolder
    "dqxclarity" = (Join-Path $PackExp "dqxclarity")
})

Remove-Item $ExpStagingCommon -Recurse -Force

# Verificacion de la estructura final de ambos zips
$vanillaTop = @(Get-ZipTopLevelNames $CommonZipOutput | Sort-Object)
$expTop     = @(Get-ZipTopLevelNames $CommonExpZipOutput | Sort-Object)
if (($vanillaTop -join ",") -ne "common,dqxclarity") {
    throw "common.zip (vanilla) tiene una estructura inesperada: $($vanillaTop -join ', ') (se esperaba common, dqxclarity)"
}
if (($expTop -join ",") -ne "common,dqxclarity,plugins") {
    throw "common_exp.zip (experimental) tiene una estructura inesperada: $($expTop -join ', ') (se esperaba common, dqxclarity, plugins)"
}
foreach ($zp in @($CommonZipOutput, $CommonExpZipOutput)) {
    $zz = [System.IO.Compression.ZipFile]::OpenRead($zp)
    try {
        $names = @($zz.Entries | ForEach-Object { $_.FullName })
        foreach ($need in @("dqxclarity/json/subPackage02Client.json", "dqxclarity/json/smldt_msg_pkg_NPC_DB.json",
                            "dqxclarity/json/subPackage05Client.json", "dqxclarity/json/subPackage41Client.json",
                            "dqxclarity/json/eventTextSysQuestaClient.json", "dqxclarity/json/eventTextSysEventaClient.json",
                            "dqxclarity/custom-translations/csv/glossary.csv", "dqxclarity/custom-translations/csv/merge.xlsx")) {
            if ($names -notcontains $need) { throw "$(Split-Path $zp -Leaf) no contiene $need" }
        }
    }
    finally { $zz.Dispose() }
}
Write-Host "common.zip generado (Vanilla): [$($vanillaTop -join ', ')] -> clean-encoding, sin plugins" -ForegroundColor Green
Write-Host "common_exp.zip generado (Experimental): [$($expTop -join ', ')] -> texto sin limpiar, con plugins" -ForegroundColor Green

# Copiamos el readme del repo (estatico, lo editas tu directamente en el
# repositorio) junto al resto de los entregables de esta corrida.
Copy-Item (Join-Path $PSScriptRoot "readme.md") (Join-Path $EtpWorkDir "etp_output\readme.md") -Force

if (-not $FromSnapshot) {
    Write-Host "`n=== Paso 8: comprimir source_cache (snapshot para fallback) ===" -ForegroundColor Cyan
    $SourceCacheZip = Join-Path $EtpWorkDir "source_cache.zip"
    if (Test-Path $SourceCacheZip) { Remove-Item $SourceCacheZip }
    Compress-Archive -Path (Join-Path $SourceCacheDir "*") -DestinationPath $SourceCacheZip
    Write-Host "source_cache.zip generado en: $SourceCacheZip" -ForegroundColor Green
}

Write-Host "`n=== Listo ===" -ForegroundColor Green
Write-Host "common.zip (vanilla) generado en: $CommonZipOutput"
Write-Host "common_exp.zip (experimental) generado en: $CommonExpZipOutput"
Write-Host "(los .clpk los genera el workflow con scripts/build_clpk.py)"