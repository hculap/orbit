'use strict';
// Zero-dep unit tests for the pure project-grouping logic (run with
// `node --test tests/projects-group.test.js`). The .jsx view is thin rendering
// on top of `groupProjectsByArea`; no jsdom/bundler in this CDN-React repo, so
// the logic lives in plain JS and is verified here directly.
const test = require('node:test');
const assert = require('node:assert/strict');
const { groupProjectsByArea } = require('../src/orbit/static/projects-group.js');

const AREAS = [
  { name: 'Dom', label: 'Dom', icon: '🏠' },
  { name: 'Praca', label: 'Praca', icon: '💼' },
];

function proj(name, linked_areas, extra) {
  return Object.assign({ name, lib_id: name, label: name, linked_areas }, extra || {});
}

test('project linked to one area lands only in that group, in areas order', () => {
  const groups = groupProjectsByArea([proj('a', ['Dom'])], AREAS, '');
  assert.equal(groups.length, 1);
  assert.equal(groups[0].key, 'Dom');
  assert.deepEqual(groups[0].projects.map(p => p.name), ['a']);
});

test('group order follows areas order', () => {
  const groups = groupProjectsByArea(
    [proj('p', ['Praca']), proj('d', ['Dom'])],
    AREAS,
    ''
  );
  assert.deepEqual(groups.map(g => g.key), ['Dom', 'Praca']);
});

test('project linked to two areas appears in BOTH groups', () => {
  const groups = groupProjectsByArea([proj('multi', ['Dom', 'Praca'])], AREAS, '');
  assert.deepEqual(groups.map(g => g.key), ['Dom', 'Praca']);
  assert.deepEqual(groups[0].projects.map(p => p.name), ['multi']);
  assert.deepEqual(groups[1].projects.map(p => p.name), ['multi']);
});

test('unlinked project lands in the trailing __none__ bucket, always last', () => {
  const groups = groupProjectsByArea(
    [proj('orphan', []), proj('d', ['Dom'])],
    AREAS,
    ''
  );
  assert.deepEqual(groups.map(g => g.key), ['Dom', '__none__']);
  const none = groups[groups.length - 1];
  assert.equal(none.key, '__none__');
  assert.equal(none.label, 'Bez area');
  assert.deepEqual(none.projects.map(p => p.name), ['orphan']);
});

test('link to an area absent from areas is ignored → falls to __none__', () => {
  const groups = groupProjectsByArea([proj('ghost', ['Nieznane'])], AREAS, '');
  assert.deepEqual(groups.map(g => g.key), ['__none__']);
  assert.deepEqual(groups[0].projects.map(p => p.name), ['ghost']);
});

test('filterText narrows by label/name across all groups; empty groups dropped', () => {
  const groups = groupProjectsByArea(
    [proj('alpha', ['Dom']), proj('beta', ['Praca'])],
    AREAS,
    'alph'
  );
  assert.deepEqual(groups.map(g => g.key), ['Dom']);
  assert.deepEqual(groups[0].projects.map(p => p.name), ['alpha']);
});

test('empty projects → []', () => {
  assert.deepEqual(groupProjectsByArea([], AREAS, ''), []);
});

test('missing/undefined areas → all projects in __none__', () => {
  const groups = groupProjectsByArea([proj('a', ['Dom']), proj('b', [])], undefined, '');
  assert.deepEqual(groups.map(g => g.key), ['__none__']);
  assert.deepEqual(groups[0].projects.map(p => p.name), ['a', 'b']);
});

test('does NOT mutate inputs', () => {
  const projects = [proj('a', ['Dom', 'Praca']), proj('b', [])];
  const areas = AREAS.map(a => Object.assign({}, a));
  const projectsBefore = JSON.stringify(projects);
  const areasBefore = JSON.stringify(areas);
  groupProjectsByArea(projects, areas, 'a');
  assert.equal(JSON.stringify(projects), projectsBefore);
  assert.equal(JSON.stringify(areas), areasBefore);
});
