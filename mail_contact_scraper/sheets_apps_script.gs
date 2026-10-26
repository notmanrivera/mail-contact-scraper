// Recibe contacts.csv desde mail-contact-scraper y lo escribe en la hoja indicada.
// Pégalo en Extensiones > Apps Script de la hoja de cálculo y publícalo como app web.
const TOKEN = "__TOKEN__";

function doPost(e) {
  const body = JSON.parse(e.postData.contents);
  if (body.token !== TOKEN) return reply({ ok: false, error: "token incorrecto" });
  const book = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = book.getSheetByName(body.tab) || book.insertSheet(body.tab);
  const values = [body.header].concat(body.rows);
  sheet.clearContents();
  // Texto plano: que "+52..." y "0001" no se conviertan en números.
  sheet.getRange(1, 1, values.length, body.header.length).setNumberFormat("@").setValues(values);
  sheet.setFrozenRows(1);
  sheet.getRange(1, 1, 1, body.header.length).setFontWeight("bold");
  return reply({ ok: true, rows: body.rows.length });
}

function reply(payload) {
  return ContentService.createTextOutput(JSON.stringify(payload)).setMimeType(ContentService.MimeType.JSON);
}
