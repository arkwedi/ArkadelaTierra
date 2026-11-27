# Expreso Terrenal -- Paquete de traducción al español de DQX.

Expreso Terrenal es el nombre del este proyecto para visualizar una traducción de DQX en el idioma español, tomando en cuenta traducciones pre-existentes en la localización oficial de los titulos de Dragon Quest, es 100% necesario mencionar que estos releases van de la mano con el software para la fan-traducción inglesa de DQX llamado "dqxclarity", este paquete se puede facilmente instalar dentro del programa, sumado a otros archivos que son necesarios de modificar en la ruta donde se encuentre ubicado dqxclarity.
Este es un proyecto de fans para fans, es necesario añadir que existen traducciones que no serán 100% fieles a la fuente original por motivos de limitaciones de la herramienta de dqxclarity y también del juego Dragon Quest X en sí, tales como las palabras que tienen la letra "ñ", o caracteres acentuados (á-é-í-ó-ú-ü, etc), o el largo de estas habilidades, hechizos, o monstruos pre-existentes, por lo que hay ligeros retoques como "Conductor de Conjuros" pasa a "Conduceconjuros", el sufijo "Despiadado" reducido a "Desp." o "D.", entre otros.

27/11/2026: Se ha incorporado un nuevo paquete que permite las restricciones mencionadas, mas información bajo la explicación del archivo "espanol_exp.clpk.

Para contribuir a la traducción, contáctame por los siguientes medios para otorgar permisos de edición en la plataforma en la que trabajamos.
- Email: arkeienflur@gmail.com
- Discord:
- Servidor de Discord:

## Contenido de este paquete

- `espanol.clpk` -- paquete de idioma en español, los contenidos de este paquete respetan las reglas y limitaciones inflingidas por el juego.
  -  Caracteres y símbolos especiales son irreconocibles en teclado (no escribe nada o te lo escribe como un signo de interrogación).
  -  Todas las instancias donde deberían ir tildes o la letra ñ se reemplazan por su contraparte natural, y para la ñ pasa a "ni" o "ny".
  -  No se ha incorporado ningún plugin o mods que permitan características en particular.
- `espanol_exp.clpk` -- paquete de idioma en español, posee el subtitulo experimental, contiene algunos añadidos por plugins.
  -  Permite la escritura y lectura de caracteres especiales, (áéíóú, ñ, entre otros)
  -  En contenido solo de lectura como interfaz, cinemáticas, dialogos de NPC se verán los caracteres correspondientes.
  -  En el caso de interactuar con otros jugadores durante el chat, los caracteres especiales consistirán en un reemplazo de kanjis antiguos sin uso, por lo que si juegas con alguien mas que no tiene el paquete instalado, solo se verán los kanjis que están reemplazando. (por ejemplo:  ñ → 鷂, ó → 鷦, ¿ → 鳬, ¡ → 鳧). Cabe mencionar que presionando Alt+K puedes activar y desactivar la visibilidad de dichos caracteres desde tu punto de vista.
  -  Existen otras funcionalidades como paginación, desplazar texto, modificación de tamaño de texto, integración y edición de imágenes, pero por el momento hemos habilitado solamente la lectura y escritura de caracteres especiales.
  
## Instalación rapida

1. Descarga "espanol.clpk" o "espanol_exp.clpk" de lanzamiento nightly mas reciente.
2. Dirigete a https://github.com/dqx-translation-project/dqxclarity/releases/latest y descarga dqxclarity. Si ya eres un jugador recurrente de DQX salta este paso porque seguro ya lo tienes.
3. Al ejecutar dqxclarity, y haber hecho su configuración respectiva (se instala python, algunas dependencias, ingresar tu código API de traducción, etc), dirigete a la pestaña "Language" y selecciona "Load from File...", escoge "espanol.clpk", espera que aparezca en el listado de idiomas, desactiva la traducción inglesa y deja solamente la española activa. 
4. De ahora en adelante para tomar las actualizaciones recientes, el mismo dqxclarity se encargará de ello ya que tiene un botón dedicado que dice "Check for updates", sumado de una checkbox que permite descargas automáticas, esperas unos segundos y se tomarán los cambios del nighly mas reciente de este repositorio.

## Repositorio

https://github.com/arkwedi/Expreso-Terrenal
