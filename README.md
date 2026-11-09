# ArkadelaTierra -- Paquete de traducción al español de DQX.

ArkadelaTierra es el nombre del este proyecto para visualizar una traducción de DQX en el idioma español, tomando en cuenta traducciones pre-existentes en la localización oficial de los titulos de Dragon Quest, es 100% necesario mencionar que estos releases van de la mano con el software para la fan-traducción inglesa de DQX llamado "dqxclarity", este paquete se puede facilmente instalar dentro del programa, sumado a otros archivos que son necesarios de modificar en la ruta donde se encuentre ubicado dqxclarity.
Este es un proyecto de fans para fans, es necesario añadir que existen traducciones que no serán 100% fieles a la fuente original por motivos de limitaciones de la herramienta de dqxclarity y también del juego Dragon Quest X en sí, tales como las palabras que tienen la letra "ñ", o caracteres acentuados (á-é-í-ó-ú-ü, etc), o el largo de estas habilidades, hechizos, o monstruos pre-existentes, por lo que hay ligeros retoques como "Cortasueños" pasando a ser "Mal Despertar", "Leñasaurio" a "Tajasaurio", etc. 

Para contribuir a la traducción, contactame por correo o DMs, para darte permisos de ediciones en la plataforma en la que trabajamos.

## Contenido de este paquete

- `espanol.clpk` -- paquete de idioma para importar directamente en dqxclarity
  (Language tab -> Load from File...).
- `paste_in_dqxclarity.zip` -- reemplazos opcionales para tu instalacion local
  de dqxclarity, para pruebas/desarrollo:
  - `dqxclarity/main.py` -- version con el auto-refresh del glosario en ingles
    desactivado (necesario si tambien vas a editar `misc_files/glossary.db` y
    `clarity_dialog.db` a mano).
  - `dqxclarity/original_main.py` -- copia sin modificar, para comparar o revertir.
  - `dqxclarity/misc_files/glossary.db`, `clarity_dialog.db` -- version en
    espanol de estas bases de datos locales de Clarity.

## Instalacion rapida

1. Descarga "espanol.clpk" y el zip "paste_in_dqxclarity.zip" de la release más reciente.
2. Dirigete a https://github.com/dqx-translation-project/dqxclarity/releases/latest y descarga dqxclarity. Si ya eres un jugador recurrente de DQX salta este paso porque seguro ya lo tienes.
3. Al ejecutar dqxclarity, y haber hecho su configuración respectiva (se instala python, algunas dependencias, ingresar tu código API de traducción, etc), dirigete a la pestaña "Language" y selecciona "Load from File...", escoge "espanol.clpk", espera que aparezca en el listado de idiomas, desactiva la traducción inglesa y deja solamente la española activa. (De ahora en adelante para tomar las actualizaciones recientes el mismo dqxclarity tiene un boton dedicado que dice "Check for updates", la tomará del nighly mas reciente de este repositorio.)
4. Descomprime `paste_in_dqxclarity.zip` y copia su contenido sobre tu carpeta de instalacion de dqxclarity, reemplazando los archivos existentes, este paso debes hacerlo después de cada nueva release, actualmente no hay forma de automatizar el glosario.

## Repositorio

https://github.com/arkwedi/dqx-hispano
