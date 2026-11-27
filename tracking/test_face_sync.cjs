// SPDX-FileCopyrightText: 2026 System Rich contributors
// SPDX-License-Identifier: AGPL-3.0-only

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const script = fs.readFileSync(require.resolve('./face-sync.gs'), 'utf8');
const header = ['БАЗА', 'Face', 'Лист Face'];
const pairs = [['base-a', 'face-a', 0], ['base-b', 'face-b', 1]];

function runRegistry(entries, { wrongRoute = false } = {}) {
  const writes = [];
  const errors = [];
  let released = 0;
  const context = vm.createContext({
    LockService: { getScriptLock: () => ({ waitLock() {}, releaseLock() { released++; } }) },
    SpreadsheetApp: {
      flush() {},
      openById: id => ({ id, getSheetByName: () => ({ getSheetId: () => 8 }) })
    },
    PropertiesService: { getScriptProperties: () => ({ getProperty: () => 'registry' }) },
    Sheets: { Spreadsheets: { Values: { get: (id, range) => {
      assert.equal(id, 'registry');
      assert.equal(range, "'Пары'!A:C");
      return { values: [header, ...entries] };
    } } } },
    console: { error: message => errors.push(message) }
  });
  vm.runInContext(script, context);
  context.faceSheet_ = file => ({ ...file, title: 'События', fileTitle: file.id });
  context.faceRoutes_ = base => {
    const isFirst = base.id === 'base-a';
    const category = isFirst ? 'Приём пищи' : 'Практики';
    const replaced = isFirst && wrongRoute;
    const id = isFirst && !replaced ? 'face-a' : 'face-b';
    return [{ id, name: id, gid: id === 'face-a' ? 0 : 1, categories: new Set([category]) }];
  };
  context.faceRows_ = source => [
    [source.id, 0, { stringValue: 'Приём пищи' }],
    [source.id, 1, { stringValue: 'Практики' }]
  ];
  context.writeFace_ = (target, rows) => writes.push([target.id, rows[0][0], rows[0][1]].join(':'));
  const run = () => vm.runInContext('syncRegisteredFaces()', context);
  return { run, writes, errors, released: () => released };
}

test('copies only each registered base and selected category without RX', () => {
  const pilot = runRegistry(pairs);
  pilot.run();
  assert.deepEqual(pilot.writes, ['face-a:base-a:0', 'face-b:base-b:1']);
  assert.equal(pilot.released(), 1);
});

test('rejects an edited route to an unrelated Face and continues with the next pair', () => {
  const pilot = runRegistry(pairs, { wrongRoute: true });
  assert.throws(pilot.run, /не подтверждены обновления витрин — 1/);
  assert.deepEqual(pilot.writes, ['face-b:base-b:1']);
  assert.equal(pilot.errors.length, 1);
  assert.equal(pilot.released(), 1);
});

test('rejects aliases and duplicate destinations before writing to any workbook', () => {
  for (const bad of [['base-b', 'face-a', 1], ['face-a', 'face-b', 1], ['base-b', 'registry', 0]]) {
    const pilot = runRegistry([pairs[0], bad]);
    assert.throws(pilot.run, /Неверная пара/);
    assert.deepEqual(pilot.writes, []);
    assert.equal(pilot.released(), 1);
  }
});

test('personal Face continues after one write fails and omits private names from diagnostics', () => {
  const logs = [];
  const writes = [];
  let released = 0;
  let firstUnavailable = true;
  const context = vm.createContext({
    LockService: { getScriptLock: () => ({ waitLock() {}, releaseLock() { released++; } }) },
    SpreadsheetApp: { flush() {}, openById: () => ({ getSheetByName: () => ({ getSheetId: () => 0 }) }) },
    PropertiesService: { getScriptProperties: () => ({ getProperty: () => 'base' }) },
    console: { log: line => logs.push(line), error: line => logs.push(line) }
  });
  vm.runInContext(script, context);
  const category = 'Приём пищи';
  const routes = [
    { name: 'private-title-a', id: 'private-id-a', gid: 1, categories: new Set([category]) },
    { name: 'private-title-b', id: 'private-id-b', gid: 2, categories: new Set([category]) }
  ];
  context.faceRoutes_ = () => routes;
  context.faceSheet_ = file => ({ ...file, title: 'События', fileTitle: file.name });
  context.faceRows_ = () => [[{ numberValue: 48000 }, null, { stringValue: category }]];
  context.writeFace_ = (target, rows) => {
    if (target.gid === 1 && firstUnavailable) throw new Error('private-provider-details');
    writes.push([target.gid, rows.length]);
  };
  const run = () => vm.runInContext('syncFace()', context);
  assert.throws(run, /не подтверждено для витрин — 1/);
  assert.deepEqual(writes, [[2, 1]]);
  assert.ok(logs.every(line => !line.includes('private-')));

  firstUnavailable = false;
  routes[0].categories.clear();
  run();
  assert.deepEqual(writes, [[2, 1], [1, 0], [2, 1]]);
  assert.equal(released, 2);
});

test('revocation clears previously published rows and keeps the header outside the write range', () => {
  let old = [[{ numberValue: 48000 }, null, { stringValue: 'Приём пищи' }]];
  let applied = 0;
  const context = vm.createContext({
    Sheets: { Spreadsheets: { batchUpdate: ({ requests }, id) => {
      assert.equal(id, 'face');
      assert.equal(requests.length, 1);
      const { range, rows, fields } = requests[0].updateCells;
      assert.equal(range.sheetId, 8);
      assert.equal(range.startRowIndex, 1);
      assert.equal(range.endRowIndex, 2);
      assert.equal(range.startColumnIndex, 0);
      assert.equal(range.endColumnIndex, 10);
      assert.equal(rows.length, 0);
      assert.equal(fields, 'userEnteredValue');
      old = [];
      applied++;
    } } }
  });
  vm.runInContext(script, context);
  context.faceRows_ = () => old;
  for (let i = 0; i < 2; i++) {
    vm.runInContext("writeFace_({id: 'face', gid: 8, rows: 100}, [])", context);
  }
  assert.equal(applied, 1);
});
