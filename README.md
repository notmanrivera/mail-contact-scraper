# mail-contact-scraper

Lee todos los correos de un buzón por IMAP, aísla la firma de cada remitente
(incluidas las firmas pegadas como imagen, vía OCR) y genera un CSV con un
contacto por persona: nombre, puesto, empresa, teléfonos, correos y sitio web.

*Reads every mail in an IMAP mailbox, isolates each sender's signature (text or
image via OCR) and writes one CSV row per contact.*

## Qué hace

1. Se conecta por IMAP en **solo lectura** (`BODY.PEEK[]`): no marca nada como leído.
2. Descarga en bloques de 50 con pausa entre bloques para no ser bloqueado.
3. Por cada correo:
   - quita la respuesta citada (`El ... escribió:`, `-----Mensaje original-----`,
     `De: / Enviado:` de Outlook, `>`, bloques de Gmail/Outlook/Apple Mail);
   - encuentra la firma: delimitador `-- `, despedida (`Saludos`, `Atentamente`,
     `Best regards`...), el nombre del remitente o el bloque final con datos de contacto;
   - descarta avisos de confidencialidad y "Enviado desde mi iPhone";
   - si la firma tiene imágenes (incrustadas `cid:` o `data:`), les aplica OCR con
     Tesseract y usa ese texto. Ignora íconos pequeños y fotos grandes;
   - valida teléfonos con `libphonenumber` (formato E.164, +52 por defecto) y los
     clasifica en móvil / oficina / fax según la etiqueta (`Cel`, `Tel`, `Fax`...).
4. Une todo por dirección de correo y escribe `output/contacts.csv` (cada 50 correos y al final).

El progreso se guarda en `output/messages.jsonl`. Si el proceso se corta, al volver a
correrlo continúa donde se quedó; correrlo después solo procesa correos nuevos.

## Instalación

```bash
git clone https://github.com/notmanrivera/mail-contact-scraper
cd mail-contact-scraper
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # y llena IMAP_USER / IMAP_PASSWORD
```

Tesseract (para firmas en imagen):

- macOS: `brew install tesseract tesseract-lang`
- Ubuntu/Debian: `sudo apt install tesseract-ocr tesseract-ocr-spa`
- Windows: instalador de https://github.com/UB-Mannheim/tesseract/wiki (marca "Spanish")

Sin Tesseract el programa funciona igual; solo se salta las firmas en imagen.

## Uso

```bash
python -m mail_contact_scraper --detect          # muestra qué servidor IMAP detectó
python -m mail_contact_scraper --list-folders    # lista las carpetas del buzón
python -m mail_contact_scraper --limit 100       # prueba con 100 correos
python -m mail_contact_scraper                   # todo el INBOX
python -m mail_contact_scraper --folders ALL     # todas las carpetas
python -m mail_contact_scraper --fresh           # empezar de cero
python -m mail_contact_scraper --local export.mbox carpeta_con_eml/   # sin IMAP
python -m mail_contact_scraper --mailbox ventas@dindustrimex.com       # solo un buzón
```

## Varios buzones

Agrega cada buzón extra al `.env` con el número al final:

```
IMAP_USER_2=ventas@dindustrimex.com
IMAP_PASSWORD_2=xxxx xxxx xxxx xxxx
IMAP_USER_3=compras@dindustrimex.com
IMAP_PASSWORD_3=xxxx xxxx xxxx xxxx
```

También se pueden agregar desde el dashboard (botón de engrane ▸ Buzones): prueba la
conexión antes de guardarlo en `.env`. Cada uno necesita su propia contraseña de aplicación. Si no pones `IMAP_HOST_2`,
`IMAP_PORT_2` o `IMAP_FOLDERS_2`, se usan los del buzón principal.

- Se leen uno tras otro y todo va al mismo `output/contacts.csv`: si un contacto escribió a
  dos buzones queda una sola fila, y la columna `mailboxes` dice en cuáles apareció.
- Si un buzón falla (por ejemplo, contraseña incorrecta), se salta y siguen los demás. El
  error aparece en el dashboard junto a ese buzón.
- El avance se guarda por buzón, así que agregar uno nuevo solo lee ese.
- `--mailbox a@x.com,b@x.com` limita la corrida a esos buzones. Con `--local`,
  `--mailbox ventas@dindustrimex.com` marca de qué buzón salió la exportación de Takeout.

## Dashboard en vivo

```bash
python -m mail_contact_scraper.dashboard                # abre el dashboard; el escaneo se inicia desde ahí
python -m mail_contact_scraper --dashboard              # abre el dashboard y empieza a escanear de inmediato
```

Se abre en el navegador (`http://127.0.0.1:8765`) con el estilo del Sistema de diseño
D-Industrimex y se actualiza cada 3 segundos:

- **Iniciar escaneo / Buscar correos nuevos** arranca el scraper con la configuración de
  `.env`. Si ya hubo una corrida, solo procesa lo nuevo.
- **Detener** lo para y guarda el avance; al volver a iniciar sigue donde se quedó.
- Avance de la carpeta con tiempo estimado, el estado de cada buzón, total de contactos, cuántos tienen teléfono y
  empresa, empresas con más contactos y una tabla con búsqueda, filtros y la firma completa
  de cada contacto al hacer clic.
- Con varios buzones aparece un filtro para ver los contactos de cada uno.
- Si el escaneo falla (contraseña, `.env` incompleto), el error aparece en la página. El
  detalle queda en `output/scraper.log`.

El logo se toma de `mail_contact_scraper/static/logo.svg` (copia ahí el rojo horizontal
oficial; ver `static/LEEME.md`). Sin ese archivo se muestra el nombre en Montserrat.

Solo escucha en tu computadora (127.0.0.1) y los botones rechazan peticiones de otras
páginas, así que los datos no se publican en ningún lado. Ctrl+C cierra el dashboard y
detiene el escaneo que haya iniciado.

### Lo que no es prospecto

Se omiten solos:

- **Servicios y apps**: Uber, Didi, Rappi, bancos, tiendas en línea, redes sociales, SAT,
  telefonía, software (lista en `mail_contact_scraper/services.txt`). A propósito no están
  empresas grandes que también pueden ser clientes (Amazon, Walmart, DHL, aerolíneas): de ellas
  se omiten solo los avisos automáticos.
- **Correos masivos**: boletines y promociones (encabezados `List-Unsubscribe-Post`,
  `Precedence: bulk`, `Auto-Submitted` o de plataformas de envío). Los grupos de Google
  como ventas@ no cuentan como masivos.
- **Remitentes automáticos**: `no-reply@`, `notificaciones@`, `avisos@`, `marketing@`...

Si se cuela alguno, ábrelo en la tabla y pulsa **Ignorar este correo** o **Ignorar todo
@dominio**. Se guarda en `ignorados.txt` (junto a `.env`), se quita del CSV en ese momento y
se deshace en Configuración ▸ Ignorados. `SKIP_SERVICES=false` apaga la lista integrada.
Lo de correos masivos solo aplica a correos leídos desde esta versión.

Configuración ▸ Ignorados ▸ **Ver la lista completa** abre una ventana con cinco pestañas:

- **Tu lista**: lo que ignoras, cuántos contactos tapa cada uno y **Dejar de ignorar**. Se
  pueden pegar varios a la vez separados por coma o en renglones: `uber.com` (el dominio y
  sus subdominios), `rappi.*` (cualquier terminación: .com, .com.mx...) o un correo.
- **Servicios**: la lista integrada por categoría (apps, bancos, tiendas, gobierno, software)
  y cuántos contactos tapa cada servicio. **Agregar servicio** suma los que falten y
  **Desactivar** apaga uno integrado que sí es cliente. Se guarda en `servicios.txt`
  (`SERVICES_FILE`); una línea `-banorte.*` es un servicio integrado desactivado.
- **Omitidos**: cada correo que se quedó fuera, por qué (servicio, masivo, automático o tu
  lista y qué regla lo tapó), cuántas veces apareció y cuándo. Se filtra, se busca y se
  descarga en CSV. Se guarda en `output/omitted.json` cada vez que se arma el CSV.
- **Permitidos**: correos o dominios que siempre son contactos aunque parezcan servicio,
  aviso o masivo (un banco que también es cliente). Se guardan en `permitidos.txt` y ganan
  sobre todo lo demás menos los correos de la propia empresa. **Permitir** en Omitidos los
  agrega aquí; ignorar algo lo quita de permitidos y al revés.
- **Bajas y rebotes**: la lista del mailing (`output/suppression.txt`), solo para consulta.
  Siguen como contactos, pero el mailing nunca les vuelve a enviar.

### Rebotes y clientes dormidos

- Los avisos de "no se pudo entregar" marcan al contacto en la columna `bounced` (insignia
  REBOTÓ en el dashboard, filtro Rebotados). El mailing ya no les envía. Si después esa
  persona vuelve a escribir, la marca se quita sola.
- La vista **Dormidos** muestra las empresas sin correos en 3, 6 o 12 meses, y **Descargar
  lista** baja sus contactos (sin los rebotados) en CSV para llamarles.

### Contactos de la propia empresa

Por defecto se omiten los contactos de los dominios de los buzones (`@dindustrimex.com` y sus
subdominios), porque son compañeros, no prospectos. Para cambiarlo, en `.env`:
`EXCLUDE_DOMAINS=dindustrimex.com,otrodominio.mx`, o `EXCLUDE_DOMAINS=none` para no omitir
ninguno. Se aplica al CSV completo cada vez que se reescribe, también a lo ya escaneado.

## Escaneo automático diario (Mac)

Desde el dashboard (engrane ▸ Escaneo automático) o con:

```bash
python -m mail_contact_scraper.schedule install --at 08:00   # todos los días a las 8:00
python -m mail_contact_scraper.schedule status
python -m mail_contact_scraper.schedule uninstall
```

Crea una tarea de `launchd` en `~/Library/LaunchAgents`. Corre aunque el dashboard esté
cerrado y, si la Mac estaba dormida, al despertar. El registro queda en `output/scheduled.log`.
Si el proyecto está en Escritorio, Documentos o Descargas, macOS no deja que la tarea lo lea:
muévelo (por ejemplo a `~/mail-contact-scraper`) o da Acceso total al disco a Python.
Nunca corren dos escaneos a la vez sobre la misma carpeta `output/`.

## Google Sheets

Manda `contacts.csv` a una pestaña de una hoja de cálculo al terminar cada escaneo, sin
proyecto de Google Cloud:

1. Dashboard ▸ engrane ▸ Google Sheets ▸ **Copiar script** (o `python -m mail_contact_scraper.sheets setup`).
   Esto crea `SHEETS_TOKEN` en `.env`.
2. En la hoja: Extensiones ▸ Apps Script, pega el script y guarda.
3. Implementar ▸ Nueva implementación ▸ App web. Ejecutar como: tú. Acceso: cualquier usuario.
4. Pega la URL (`https://script.google.com/macros/s/.../exec`) en el dashboard y pulsa
   **Conectar y enviar**, o ponla en `SHEETS_WEBAPP_URL` y corre `python -m mail_contact_scraper.sheets push`.

Cada envío reemplaza la pestaña `Contactos` (cámbiala con `SHEETS_TAB`). Los teléfonos van
como texto. `SHEETS_AUTO=false` apaga el envío automático. Si tu Workspace no permite
"cualquier usuario", pídele al admin que lo habilite para apps web; si no, sube el CSV a mano.
El token evita que alguien más escriba en tu hoja: no lo compartas.

## App de Mac (para compartir con la oficina)

Para quien no usa la terminal: **Contactos D-Industrimex.app** trae Python, las librerías y
Tesseract adentro. Se instala arrastrándola a Aplicaciones y al abrirla pone un sobre en la
barra de menú y abre el panel en el navegador. Los buzones se agregan desde el panel
(Configuración ▸ Buzones).

- **Dónde se guardan los datos:** `~/Library/Application Support/Contactos D-Industrimex/`
  (`.env` con los buzones y contraseñas, `output/` con `contacts.csv`). Cada usuario de la Mac
  tiene los suyos; la app no lleva ninguna credencial. Menú del sobre ▸ *Abrir carpeta de
  contactos* abre `output/`.
- **Cómo se publica:** desde una Mac con Apple Silicon, `packaging/release_local.sh` (ver
  abajo) arma lo que está en `main`, lo prueba (pruebas, OCR, scraper con los correos de
  `tests/fixtures`, panel) y crea el release `v1.0.N` con `…-arm64.dmg` para instalar y
  `….app.zip` para las actualizaciones. No gasta minutos de GitHub Actions. El workflow
  `.github/workflows/mac-app.yml` hace lo mismo en GitHub, pero solo a mano (pestaña Actions ▸
  *Run workflow*), porque los minutos de macOS cuentan 10 veces en el plan gratis.
- **Actualizaciones:** la app revisa los releases al abrirse y cada media hora, lo descarga
  en segundo plano y lo instala la próxima vez que se abre o al salir (o al momento con
  *Actualizar ahora* en el panel). No actualiza a media corrida de escaneo.
  La v1.0.6 no podía conectarse por HTTPS (le faltaban los certificados); quien la tenga
  debe instalar a mano una versión nueva desde el `.dmg` una vez, y de ahí en adelante se
  actualiza sola.
- **Token de GitHub:** como el repo es privado, cada Mac necesita un token *fine-grained* de
  solo lectura (Settings ▸ Developer settings ▸ Fine-grained tokens ▸ solo este repositorio ▸
  Contents: Read-only). Se pega en Configuración ▸ Actualizaciones y se guarda en el `.env`
  de esa Mac como `UPDATE_GITHUB_TOKEN`. Cuando caduque, la app lo dice en el panel.

### Instalar en otra Mac

1. En GitHub ▸ Releases, baja el `.dmg` de la versión más nueva (`arm64`, para Macs con chip
   Apple: M1, M2…). Para una Mac con Intel hay que correr `release_local.sh` en una Mac Intel.
2. Pásalo a la otra Mac (AirDrop, USB), ábrelo y arrastra la app a Aplicaciones.
3. Abre la app. Como no está firmada con una cuenta de desarrollador de Apple (99 USD/año),
   macOS la bloquea la primera vez: Configuración del Sistema ▸ Privacidad y seguridad ▸
   *Abrir igualmente*. Solo una vez; las actualizaciones ya no lo piden.
4. En el panel: Configuración ▸ Buzones (correo + contraseña de aplicación) y
   Configuración ▸ Actualizaciones (token).

### Armarla a mano en una Mac

```bash
brew install tesseract dylibbundler
pip install -r requirements.txt -r packaging/requirements-build.txt
APP_VERSION=1.0.0 packaging/build_mac.sh       # sin APP_REPO no busca actualizaciones
open dist/
```

`python -m mail_contact_scraper.app` corre lo mismo desde el código, sin el ícono de la barra.

### Publicar una versión desde una Mac (sin GitHub Actions)

```bash
git clone https://github.com/notmanrivera/mail-contact-scraper.git   # solo la primera vez
cd mail-contact-scraper
packaging/release_local.sh
```

Instala lo que falte con Homebrew, pide entrar a GitHub la primera vez (`gh auth login`),
arma lo que está en `main`, lo prueba y publica el release `v1.0.N` siguiente (unos 5
minutos). Las apps instaladas lo encuentran solas en menos de media hora. Arma para el
procesador de esa Mac; correrlo en una Mac Intel sobre el mismo commit agrega esa versión
al mismo release.

## Firmas en imagen

Las firmas pegadas como imagen se leen con Tesseract en la misma computadora. Para
cada imagen se hacen dos lecturas (texto suelto y por renglones), se descartan las
palabras de las que Tesseract no está seguro y los pedazos de íconos, y se juntan
los renglones sin repetir. El texto claro sobre fondo oscuro se invierte antes de leerlo.

Para las difíciles, como texto blanco sobre la foto de un camión, puedes usar Claude
(Configuración ▸ Firmas en imagen, o en `.env`):

```
OCR_ENGINE=auto            # Claude solo cuando Tesseract duda; "claude" para todas
ANTHROPIC_API_KEY=sk-ant-...
```

- Viene apagado porque manda la imagen de la firma a Anthropic.
- Cada imagen nueva cuesta de 1 a 2 centavos de dólar con `claude-opus-5-5`
  (`OCR_CLAUDE_MODEL` cambia el modelo). Las respuestas se guardan en
  `output/ocr_claude.json`, así que la misma imagen no se paga dos veces.
- Si Claude falla (sin internet, sin saldo), se queda lo que leyó Tesseract.

Los correos que ya se leyeron guardan el texto de la lectura anterior. Para aplicar
las mejoras a esos, usa **Releer firmas en imagen** en Configuración, o
`python -m mail_contact_scraper --reocr`: vuelve a bajar solo los correos cuya firma
era imagen.

## Volver a leer todos los correos

Cuando la app aprende a leer mejor (OCR, nombres, correos masivos), los correos ya leídos
se quedan con la lectura vieja. Configuración ▸ **Volver a leer todo**, o
`python -m mail_contact_scraper --refresh`, los baja y lee otra vez:

- Los contactos actuales se quedan mientras tanto; cada correo se reemplaza al releerlo.
- Si lo detienes, al volver a pulsarlo sigue con los que faltan (cada registro guarda con
  qué versión se leyó, `v` en `messages.jsonl`).
- No toca el registro de envíos del mailing, las bajas ni los rebotes.
- Los ignorados, servicios y permitidos no lo necesitan: se aplican al momento a todo lo
  ya leído cada vez que se arma el CSV.

## Contraseña según el proveedor

El servidor se detecta con los registros MX de `dindustrimex.com`. Si la detección
falla, pon `IMAP_HOST` en `.env`.

| Proveedor | IMAP_HOST | Credencial |
|---|---|---|
| Google Workspace / Gmail | `imap.gmail.com` | Activa verificación en 2 pasos y crea una [contraseña de aplicación](https://myaccount.google.com/apppasswords). IMAP ya viene activo (Google quitó el botón). Si la página dice "no disponible", el admin del Workspace debe permitir la verificación en 2 pasos; mientras tanto puedes exportar con Google Takeout y usar `--local archivo.mbox`. |
| Microsoft 365 / Outlook | `outlook.office365.com` | Microsoft desactivó la contraseña para IMAP. Se necesita un token OAuth2 en `IMAP_OAUTH_TOKEN` (app registrada en Entra ID con permiso `IMAP.AccessAsUser.All`). |
| Hosting cPanel / Hostinger / GoDaddy / Zoho | `mail.<dominio>`, `imap.hostinger.com`, `imap.secureserver.net`, `imap.zoho.com` | La contraseña normal del buzón. |

## Columnas del CSV

`email, name, title, company, location, mobile_phones, office_phones, fax, other_emails,
websites, source, messages, first_seen, last_seen, sample_signature, mailboxes, bounced`

- `source`: `signature` (salió de una firma), `image` (de una firma en imagen vía OCR)
  o `header` (solo aparece en De/Para/CC).
- `mailboxes`: los buzones donde apareció el contacto.
- `location`: ciudad y estado según la lada del teléfono (primero el de oficina), por
  ejemplo `Monterrey, NL`. Si la lada no dice la ciudad, sale solo el estado o el país.
- Varios valores van separados por `; `. El archivo usa UTF-8 con BOM para que Excel
  muestre bien los acentos.

## Mailings a prospectos (`python -m mailing`)

Genera el mailing con la imagen de D-Industrimex (Montserrat, rojo #A81818, fondo acero,
barra de sitio) personalizado para cada contacto de `output/contacts.csv`. **No envía nada.**

```bash
python -m mailing --excel                 # Excel para la macro de Outlook
python -m mailing --esp brevo             # o mailchimp: plantilla + CSV para importar
python -m mailing --eml                   # un borrador .eml por prospecto
python -m mailing --require-signature     # solo contactos que tienen firma
```

Sale en `output/mailing/<campaña>/`: `preview.html` (ábrelo primero), `audience.csv` y lo
que pidas. Quita automáticamente correos inválidos, los de `@dindustrimex.com` y los que
estén en `output/suppression.txt` (uno por línea; ahí van las bajas).

- **Campaña nueva:** copia `mailing/campaigns/qc-aro.toml` y cambia el copy. Lo que va entre
  `**dobles asteriscos**` sale en rojo. Una sola marca aliada por pieza con logo; si hay dos,
  van como badge rojo con el nombre (así está la de QC + ARO).
- **Imágenes:** viven en `mailing/assets/<campaña>/` (logos y fotos reales del banco de
  recursos, ya optimizadas). Con `base_url` vacío van **incrustadas** en el correo: la macro
  las adjunta desde la carpeta `imagenes/` que se genera junto al Excel, y los `.eml` ya las
  traen. Para Brevo/Mailchimp súbelas a un servidor y pon esa carpeta en `[assets] base_url`.
- **Envío propio desde el buzón (gratis, sin YAMM):** usa el mismo buzón y contraseña de
  aplicación que el scraper (`IMAP_USER` en `.env`, el del `sender` de la campaña) por SMTP,
  así que cada correo sale de pedro@ y queda en sus Enviados.
  ```bash
  python -m mailing.send                        # cuántos faltan, no envía nada
  python -m mailing.send --test tu@correo.com   # una prueba con asunto [PRUEBA]
  python -m mailing.send --send                 # envía; pide escribir ENVIAR
  python -m mailing.send --bajas                # solo revisa bajas y rebotes
  ```
  - **Desde la app:** el botón del sobre (arriba, junto a Configuración) abre *Envíos*.
    - *Campaña*: cuántos hay en la lista, cuántos ya la recibieron, cuántos faltan y cuántos
      quedan fuera por baja o rebote; qué buzones envían (se turnan) y su cupo de hoy; el
      registro de envíos y el botón *Revisar bajas y rebotes*. Para enviar hay que escribir
      ENVIAR; se ve el avance y se puede detener. Antes de enviar revisa bajas y rebotes, y
      mientras envía la Mac no se duerme. Usa los mismos archivos que `mailing.send`.
    - *Prueba*: trae el HTML y asunto de la campaña (o pega otro HTML), muestra la vista previa
      y manda un solo correo con [PRUEBA] desde el buzón que elijas. No toca `envios.csv`.
  - Cada envío queda en `output/mailing/<campaña>/envios.csv` (fecha, correo, estado). Al
    volver a correrlo sigue con los pendientes y nunca repite a nadie.
  - Límites: 1,500 al día por buzón (`--daily-limit`), con 3 a 6 s entre correos (`--pause`)
    y una pausa de 30 a 90 s cada ~50; `--limit` corta la corrida antes. Google Workspace
    permite 2,000 al día por usuario, pero para correo en frío conviene 300–500 por buzón y
    repartir en varios buzones. Si Google frena un buzón, ese buzón se detiene y los demás siguen.
  - Conexión: abre una sesión SMTP nueva cada 50 correos (Gmail corta las largas con
    `421 4.7.0 Connection expired`). Si se corta o responde 4xx, reconecta y reintenta el mismo
    correo tras 10, 30 y 60 s; nadie queda marcado como fallido por eso.
  - **Más volumen con varios buzones:** `--from` se repite y los buzones se turnan; cada uno
    lleva su propio cupo diario y su contraseña de aplicación en `.env` (`IMAP_USER_2`...):
    ```bash
    python -m mailing.send --send --from pedro@dindustrimex.com --from ventas@dindustrimex.com
    ```
  - **Miles al día con un servidor de envío** (Amazon SES ~US$0.10 por 1,000 correos, o el
    SMTP de Brevo): pon `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` y
    `SMTP_DAILY_LIMIT` en `.env`. Todo sale por ahí con el remitente de la campaña (el dominio
    debe estar verificado en ese servicio, con SPF y DKIM en el DNS).
  - Calienta primero: unos 200 diarios por buzón la primera semana y sube poco a poco. Con
    dindustrimex.com conviene tener SPF, DKIM y DMARC; Gmail los exige a quien manda volumen.
  - Antes de cada envío lee la bandeja (sin marcar nada como leído): quien respondió con asunto
    "Baja de correos" (la liga de baja del correo lo arma así) y los rebotes de mailer-daemon
    pasan a `output/suppression.txt`. Para que vea las bajas que llegan a ventas@, agrega ese
    buzón como `IMAP_USER_2`. Los rechazados por el servidor también se agregan.
- **Envío con YAMM (Google Sheets + Gmail), alternativa de pago:**
  1. Sube las imágenes de `mailing/assets/<campaña>/` al Media Manager de Wix
     (dindustrimex.com), copia la URL de cada una y pégalas en `[assets.urls]` de la campaña.
  2. `python -m mailing --esp yamm` → `template-yamm.html` y `audience-yamm.csv`.
  3. Abre `template-yamm.html` en Chrome, Cmd/Ctrl+A, Cmd/Ctrl+C y pégalo en un borrador nuevo
     de Gmail (en la cuenta que va a enviar) con el asunto de la campaña.
  4. En el Sheet: Archivo ▸ Importar ▸ `audience-yamm.csv` ▸ Insertar hoja nueva. El saludo
     usa la columna `Saludo` (`{{Saludo}}` en el borrador).
  5. Extensiones ▸ YAMM ▸ Iniciar combinación ▸ elige el borrador, activa la liga de baja de
     YAMM y mándate una prueba antes de enviar. Plan gratis: 50 correos al día.
- **Envío con Excel:** importa `mailing/excel/EnviarMailing.bas` en el libro generado
  (Alt+F11 ▸ Archivo ▸ Importar), con la carpeta `imagenes/` al lado. Requiere Outlook de escritorio en Windows. Arranca en modo
  `BORRADOR` (deja todo en Borradores); cámbialo a `ENVIAR` cuando hayas revisado. Envía en
  lotes con pausa y marca la columna `Enviado`, así que puede correrse varias veces.
- **Baja:** cada correo trae el enlace "Ya no quiero recibir estos correos" y el encabezado
  `List-Unsubscribe` hacia ventas@. Quien pida la baja va a `output/suppression.txt` antes
  del siguiente envío.

## Pruebas

```bash
pip install pytest
pytest
```

## Notas

- `.env` y `output/` están en `.gitignore`: credenciales y datos personales no se suben.
- Los datos extraídos son datos personales (LFPDPPP en México): úsalos solo para
  fines legítimos de la empresa.
- `OCR_REMOTE_IMAGES=true` descarga imágenes de firma alojadas en internet. Viene
  apagado porque esas descargas le avisan al remitente que abriste su correo.
