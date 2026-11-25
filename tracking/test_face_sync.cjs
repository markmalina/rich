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
