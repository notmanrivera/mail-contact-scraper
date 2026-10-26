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
```

## Dashboard en vivo

```bash
python -m mail_contact_scraper --dashboard              # escanea y abre el dashboard
python -m mail_contact_scraper.dashboard                # solo el dashboard, sobre output/
```

Se abre en el navegador (`http://127.0.0.1:8765`) y se actualiza cada 3 segundos
mientras corre el scraper: avance de la carpeta con tiempo estimado, total de contactos,
cuántos tienen teléfono y empresa, empresas con más contactos y una tabla con búsqueda,
filtros y la firma completa de cada contacto al hacer clic. El CSV se reescribe cada
50 correos, así que también puedes abrirlo a medio proceso.

Solo escucha en tu computadora (127.0.0.1); los datos no se publican en ningún lado.
Ctrl+C lo cierra.

## Contraseña según el proveedor

El servidor se detecta con los registros MX de `dindustrimex.com`. Si la detección
falla, pon `IMAP_HOST` en `.env`.

| Proveedor | IMAP_HOST | Credencial |
|---|---|---|
| Google Workspace / Gmail | `imap.gmail.com` | Activa verificación en 2 pasos y crea una [contraseña de aplicación](https://myaccount.google.com/apppasswords). IMAP ya viene activo (Google quitó el botón). Si la página dice "no disponible", el admin del Workspace debe permitir la verificación en 2 pasos; mientras tanto puedes exportar con Google Takeout y usar `--local archivo.mbox`. |
| Microsoft 365 / Outlook | `outlook.office365.com` | Microsoft desactivó la contraseña para IMAP. Se necesita un token OAuth2 en `IMAP_OAUTH_TOKEN` (app registrada en Entra ID con permiso `IMAP.AccessAsUser.All`). |
| Hosting cPanel / Hostinger / GoDaddy / Zoho | `mail.<dominio>`, `imap.hostinger.com`, `imap.secureserver.net`, `imap.zoho.com` | La contraseña normal del buzón. |

## Columnas del CSV

`email, name, title, company, mobile_phones, office_phones, fax, other_emails,
websites, source, messages, first_seen, last_seen, sample_signature`

- `source`: `signature` (salió de una firma), `image` (de una firma en imagen vía OCR)
  o `header` (solo aparece en De/Para/CC).
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
- **Envío con YAMM (Google Sheets + Gmail), lo que usa el equipo:**
  1. Sube las imágenes de `mailing/assets/<campaña>/` a una carpeta pública (p. ej.
     `https://www.dindustrimex.com/mailing/qc-aro/`) y ponla en `[assets] base_url`.
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
