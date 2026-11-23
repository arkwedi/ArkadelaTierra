# run_daily_pipeline_fallback.ps1
#
# Respaldo para cuando la PC esta apagada. Toda la logica vive en
# run_daily_pipeline.ps1; con -FromSnapshot se salta los Pasos 1-2 (etp.exe all /
# port-translations) y usa el snapshot que el workflow dejo en ETP_WORK_DIR
# (json\_lang\en, etp\, rps\, plugins\, fetch_cache\). El texto japones fuente es el
# de la ultima corrida buena; todo lo que depende de Supabase (ES nuevo, glosario,
# custom_*) y las descargas (upstream, Google Sheet) si se actualizan.

& (Join-Path $PSScriptRoot "run_daily_pipeline.ps1") -FromSnapshot
exit $LASTEXITCODE
