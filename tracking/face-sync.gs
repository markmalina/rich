// SPDX-FileCopyrightText: 2026 System Rich contributors
// SPDX-License-Identifier: AGPL-3.0-only

const FACE_HEADERS = ['Дата', 'Время (МСК)', 'Категория', 'Действие', 'Описание', 'Количество', 'Ед.', 'Длительность, мин', 'Расход, ккал', 'Тип времени'];
const FACE_ROUTE_HEADERS = ["Face","Таблица назначения","Физические упражнения","Приём пищи","Замеры","Практики","Режим дня","Чтение и обучение"];
const FACE_CATEGORIES = new Set(FACE_ROUTE_HEADERS.slice(2));

function installFaceSync() {
  const base = SpreadsheetApp.getActiveSpreadsheet();
  if (!base || !base.getSheetByName('Экспорт Faces') || !base.getSheetByName('Faces')) {
    throw new Error('Установите скрипт из меню Расширения в таблице БАЗА Rich.');
  }
  const baseId = base.getId();
  PropertiesService.getScriptProperties().setProperty('RICH_BASE_ID', baseId);
  syncFace();
  const installed = new Set(ScriptApp.getProjectTriggers()
    .filter(trigger => trigger.getHandlerFunction() === 'syncFace')
    .map(trigger => trigger.getEventType()));
  const types = ScriptApp.EventType;
  if (!installed.has(types.ON_EDIT)) ScriptApp.newTrigger('syncFace').forSpreadsheet(baseId).onEdit().create();
  if (!installed.has(types.ON_CHANGE)) ScriptApp.newTrigger('syncFace').forSpreadsheet(baseId).onChange().create();
  if (!installed.has(types.CLOCK)) ScriptApp.newTrigger('syncFace').timeBased().everyMinutes(5).create();
}

function syncFace(event) {
  if (event && event.changeType === 'EDIT') return;
  const lock = LockService.getScriptLock();
  lock.waitLock(25000);
  try {
    SpreadsheetApp.flush();
    const baseId = PropertiesService.getScriptProperties().getProperty('RICH_BASE_ID');
    if (!baseId) throw new Error('Сначала выполните installFaceSync.');
    const base = SpreadsheetApp.openById(baseId);
    const exported = base.getSheetByName('Экспорт Faces');
    if (!exported) throw new Error('В БАЗЕ отсутствует вкладка «Экспорт Faces».');
    const routes = faceRoutes_(base);
    const source = faceSheet_({ id: baseId, gid: exported.getSheetId() });
    const rows = faceRows_(source, true);
    const failed = [];
    for (const route of routes) {
      try {
        const target = faceSheet_(route);
        if (target.title !== 'События' || target.fileTitle !== route.name) {
          throw new Error('Назначение в Faces не совпадает с файлом или листом.');
        }
        const selected = rows.filter(row => route.categories.has(row[2].stringValue));
        writeFace_(target, selected);
        console.log(route.name + ': сверено строк — ' + selected.length);
      } catch (error) {
        console.error(route.name + ': ' + error.message);
        failed.push(route.name);
      }
    }
    if (failed.length) throw new Error('Обновление не подтверждено для: ' + failed.join(', '));
  } finally {
    lock.releaseLock();
  }
}

function faceRoutes_(base) {
  const routeSheet = base.getSheetByName('Faces');
  if (!routeSheet) throw new Error('В БАЗЕ отсутствует вкладка Faces.');
  const config = faceSheet_({ id: base.getId(), gid: routeSheet.getSheetId() }, FACE_ROUTE_HEADERS.length);
  const ranges = "'" + config.title.replace(/'/g, "''") + "'!A1:H" + config.rows;
  const resource = Sheets.Spreadsheets.Values.get(config.id, ranges, { valueRenderOption: 'UNFORMATTED_VALUE' });
  const [header, ...entries] = resource.values || [];
  if (config.title !== 'Faces' || JSON.stringify(header) !== JSON.stringify(FACE_ROUTE_HEADERS)) {
    throw new Error('Изменилась структура вкладки Faces.');
  }
  const used = new Set();
  const routes = [];
  for (const [name, url, ...flags] of entries) {
    if (!name && !url && !flags.some(value => value === true)) continue;
    const file = typeof url === 'string' && /^https:\/\/docs\.google\.com\/spreadsheets\/d\/([A-Za-z0-9_-]+)\/edit(?:[?#].*)?$/.exec(url);
    const tab = typeof url === 'string' && /[?#&]gid=(\d+)(?:[&#]|$)/.exec(url);
    if (typeof name !== 'string' || !name.trim() || !file || !tab || file[1] === config.id
        || used.has(file[1]) || flags.some(value => value !== '' && typeof value !== 'boolean')) {
      throw new Error('Строка назначения на вкладке Faces заполнена неверно.');
    }
    used.add(file[1]);
    routes.push({
      name: name.trim(), id: file[1], gid: Number(tab[1]),
      categories: new Set(FACE_ROUTE_HEADERS.slice(2).filter((_, index) => flags[index] === true))
    });
  }
  if (!routes.length) throw new Error('На вкладке Faces нет назначений.');
  return routes;
}

function writeFace_(target, next) {
  const old = faceRows_(target, false);
  if (JSON.stringify(next) === JSON.stringify(old)) return;
  const end = Math.max(next.length, old.length) + 1;
  const requests = [];
  if (end > target.rows) {
    requests.push({ appendDimension: { sheetId: target.gid, dimension: 'ROWS', length: end - target.rows } });
    const formats = [
      [0, { numberFormat: { type: 'DATE', pattern: 'dd.mm.yyyy' } }, 'numberFormat'],
      [1, { numberFormat: { type: 'TIME', pattern: 'hh:mm' } }, 'numberFormat'],
      [4, { wrapStrategy: 'WRAP' }, 'wrapStrategy'],
      [8, { numberFormat: { type: 'NUMBER', pattern: '0.0' } }, 'numberFormat']
    ];
    formats.forEach(([column, userEnteredFormat, key]) => requests.push({ repeatCell: {
      range: faceRange_(target.gid, target.rows, end, column, column + 1),
      cell: { userEnteredFormat }, fields: 'userEnteredFormat.' + key
    } }));
  }
  requests.push({ updateCells: {
    range: faceRange_(target.gid, 1, end, 0, FACE_HEADERS.length),
    rows: next.map(row => ({ values: row.map(value => value === null ? {} : { userEnteredValue: value }) })),
    fields: 'userEnteredValue'
  } });
  Sheets.Spreadsheets.batchUpdate({ requests }, target.id);
  const actual = faceRows_({ ...target, rows: Math.max(target.rows, end) }, false);
  if (JSON.stringify(next) !== JSON.stringify(actual)) throw new Error('Проверка записи не прошла.');
}

function faceSheet_(file, minColumns = FACE_HEADERS.length) {
  const response = Sheets.Spreadsheets.get(file.id, { fields: 'spreadsheetId,properties(title),sheets(properties(sheetId,title,sheetType,gridProperties))' });
  const sheet = (response.sheets || []).find(sheet => sheet.properties.sheetId === file.gid);
  const props = sheet && sheet.properties;
  if (response.spreadsheetId !== file.id || !props || props.sheetType !== 'GRID' || props.gridProperties.columnCount < minColumns) {
    throw new Error('Face: изменилась структура файла.');
  }
  return { ...file, title: props.title, fileTitle: response.properties.title, rows: props.gridProperties.rowCount };
}

function faceRows_(file, source) {
  const records = [];
  const sheet = "'" + file.title.replace(/'/g, "''") + "'";
  const field = source ? 'effectiveValue' : 'userEnteredValue';
  let blankSeen = false;
  for (let start = 1; start <= file.rows; start += 500) {
    const end = Math.min(start + 499, file.rows);
    const r = Sheets.Spreadsheets.get(file.id, {
      ranges: [sheet + '!A' + start + ':J' + end],
      fields: 'sheets(properties(sheetId),data(startRow,rowData(values(effectiveValue,userEnteredValue))))'
    });
    const returned = r.sheets && r.sheets[0];
    const grid = returned && returned.data && returned.data[0];
    if (!grid || returned.properties.sheetId !== file.gid || (grid.startRow || 0) !== start - 1) {
      throw new Error('Face: диапазон чтения недоступен.');
    }
    const cells = grid.rowData || [];
    if (start === 1) {
      const titles = (cells[0] && cells[0].values || []).map(cell => cell[field] && cell[field].stringValue);
      if (JSON.stringify(titles) !== JSON.stringify(FACE_HEADERS)) throw new Error('Face: изменились заголовки.');
      const anchor = cells[1]?.values?.[0]?.userEnteredValue;
      if (source && (!anchor || !anchor.formulaValue)) throw new Error('Face: отсутствует формула экспорта.');
    }
    for (let i = start === 1 ? 1 : 0; i <= end - start; i++) {
      const values = cells[i] && cells[i].values || [];
      const row = FACE_HEADERS.map((_, index) => faceValue_((values[index] || {})[field], source));
      const blank = row.every(value => value === null);
      if (source && blank) {
        blankSeen = true;
        continue;
      }
      if (source && (blankSeen || !row[0] || !Number.isFinite(row[0].numberValue) || !row[2]
          || !FACE_CATEGORIES.has(row[2].stringValue) || !row[3] || typeof row[3].stringValue !== 'string')) {
        throw new Error('Face: неверная структура или категория экспорта.');
      }
      records.push(row);
    }
    if (source && blankSeen) break;
  }
  while (records.length && records[records.length - 1].every(value => value === null)) records.pop();
  return records;
}

function faceValue_(value, source) {
  if (!value || value.stringValue === '') return null;
  if (typeof value.stringValue === 'string') return { stringValue: value.stringValue };
  if (Number.isFinite(value.numberValue)) return { numberValue: value.numberValue };
  if (source) throw new Error('Face: ошибка или недопустимое значение в экспорте.');
  return { nonliteral: true };
}

function faceRange_(sheetId, startRowIndex, endRowIndex, startColumnIndex, endColumnIndex) {
  return { sheetId, startRowIndex, endRowIndex, startColumnIndex, endColumnIndex };
}
