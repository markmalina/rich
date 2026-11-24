// SPDX-FileCopyrightText: 2026 System Rich contributors
// SPDX-License-Identifier: AGPL-3.0-only

const RX_READ = new Set([
  "'Входящие'!A1:H1", "'Входящие'!A2:A", "'События'!I1",
  "'События'!A2:O", "'События'!I2:L", "'Действия'!A2:G",
  "'Действия'!I2", "'Правило'!A7:D50"
]);

function doPost(event) {
  try {
    const raw = event && event.postData && event.postData.contents;
    if (!raw || raw.length > 1000000) return rxJson_({error: 'bad request'});
    const body = JSON.parse(raw);
    const properties = PropertiesService.getScriptProperties();
    const token = properties.getProperty('RX_BRIDGE_TOKEN');
    const spreadsheet = properties.getProperty('RX_SPREADSHEET_ID');
    if (!body || !rxAuthorized_(body.token, token) || !spreadsheet ||
        body.spreadsheet_id !== spreadsheet) return rxJson_({error: 'denied'});
    return rxJson_(rxValues_(body, spreadsheet));
  } catch (error) {
    return rxJson_({error: 'request failed'});
  }
}

function rxAuthorized_(candidate, token) {
  if (typeof candidate !== 'string' || typeof token !== 'string' ||
      !/^[a-f0-9]{64}$/.test(candidate) || !/^[a-f0-9]{64}$/.test(token)) return false;
  let difference = 0;
  for (let i = 0; i < token.length; i++) difference |= candidate.charCodeAt(i) ^ token.charCodeAt(i);
  return difference === 0;
}

function rxValues_(body, spreadsheet) {
  const range = body.range;
  if (typeof range !== 'string') return {error: 'unsupported operation'};
  if (body.operation === 'read' && (RX_READ.has(range) || /^'Входящие'!F(?:[2-9]|[1-9][0-9]{1,6})$/.test(range))) {
    const result = Sheets.Spreadsheets.Values.get(spreadsheet, range, {valueRenderOption: 'UNFORMATTED_VALUE'});
    return {values: result.values || []};
  }
  if (body.operation === 'append' &&
      ((range === "'Входящие'!A:H" && rxRows_(body.rows, 8, 1)) ||
       (range === "'События'!A:O" && rxRows_(body.rows, 15, 40)))) {
    const result = Sheets.Spreadsheets.Values.append({values: body.rows}, spreadsheet, range, {
      valueInputOption: 'RAW', insertDataOption: 'INSERT_ROWS'
    });
    return {updated_rows: result.updates.updatedRows};
  }
  if (body.operation === 'update' && /^'Входящие'![FG](?:[2-9]|[1-9][0-9]{1,6})$/.test(range) &&
      rxRows_(body.rows, 1, 1) && typeof body.rows[0][0] === 'string') {
    Sheets.Spreadsheets.Values.update({values: body.rows}, spreadsheet, range, {valueInputOption: 'RAW'});
    return {updated_rows: 1};
  }
  return {error: 'unsupported operation'};
}

function rxRows_(rows, columns, maximum) {
  return Array.isArray(rows) && rows.length > 0 && rows.length <= maximum &&
    rows.every(row => Array.isArray(row) && row.length === columns && row.every(value =>
      typeof value === 'string' || typeof value === 'boolean' || Number.isFinite(value)));
}

function rxJson_(response) {
  return ContentService.createTextOutput(JSON.stringify(response)).setMimeType(ContentService.MimeType.JSON);
}
