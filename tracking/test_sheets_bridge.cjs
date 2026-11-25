// SPDX-FileCopyrightText: 2026 System Rich contributors
// SPDX-License-Identifier: AGPL-3.0-only

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, 'sheets-bridge.gs'), 'utf8');
const token = 'a'.repeat(64);
const id = 'test-sheet';

function fixture() {
  const calls = [];
  const values = {
    get(...args) {
      calls.push(['read', ...args]);
      return {values: [['verified']]};
    },
    append(...args) {
      calls.push(['append', ...args]);
      return {updates: {updatedRows: 1}};
    },
    update(...args) {
      calls.push(['update', ...args]);
    },
  };
  const properties = {RX_BRIDGE_TOKEN: token, RX_SPREADSHEET_ID: id};
  const context = vm.createContext({
    Sheets: {Spreadsheets: {Values: values}},
    PropertiesService: {getScriptProperties: () => ({getProperty: name => properties[name]})},
    ContentService: {
      MimeType: {JSON: 'json'},
      createTextOutput: text => ({setMimeType: () => text}),
    },
  });
  vm.runInContext(source, context);
  const post = body => JSON.parse(context.doPost({postData: {contents: JSON.stringify({
    token, spreadsheet_id: id, operation: 'read', range: "'Входящие'!A1:H1", ...body,
  })}}));
  return {post, calls};
}

test('permits scoped reads and rejects an invalid key, different file, and other ranges', () => {
  const {post, calls} = fixture();
  assert.deepEqual(post(), {values: [['verified']]});
  assert.equal(calls[0][1], id);
  assert.equal(calls[0][3].valueRenderOption, 'UNFORMATTED_VALUE');
  assert.deepEqual(post({token: 'b'.repeat(64)}), {error: 'denied'});
  assert.deepEqual(post({spreadsheet_id: 'other-sheet'}), {error: 'denied'});
  assert.deepEqual(post({range: "'Входящие'!A1:Z500"}), {error: 'unsupported operation'});
  assert.equal(calls.length, 1);
});

test('writes as literal data and rejects a write to the header', () => {
  const {post, calls} = fixture();
  const rows = [['tg-fixture', 46700, 'test', '=1+1', '', '', '', '']];
  assert.deepEqual(post({operation: 'append', range: "'Входящие'!A:H", rows}), {updated_rows: 1});
  assert.equal(calls[0][1].values[0][3], '=1+1');
  assert.equal(calls[0][4].valueInputOption, 'RAW');
  assert.deepEqual(post({operation: 'update', range: "'Входящие'!F1", rows: [['bad']]}),
                   {error: 'unsupported operation'});
  assert.deepEqual(post({operation: 'update', range: "'Входящие'!F2", rows: [['transcript']]}),
                   {updated_rows: 1});
  assert.equal(calls[1][4].valueInputOption, 'RAW');
});
