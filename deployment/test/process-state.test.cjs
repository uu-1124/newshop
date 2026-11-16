const assert = require('node:assert/strict');
const test = require('node:test');
const { createStateWriter } = require('../process-state.cjs');

test('transient Windows rename denial preserves valid JSON and persists the latest child state', () => {
  let saved = '{"manager":1,"children":[]}', staged, blocked = true;
  let current = { manager: 1, children: [{ name: 'agent-api', pid: 100 }] };
  const callbacks = [], logs = [];
  const save = createStateWriter('pids.json', () => current, message => logs.push(message), {
    fs: {
      writeFileSync(file, data) { assert.equal(file, 'pids.json.tmp'); staged = data; },
      renameSync(from, to) {
        assert.equal(from, 'pids.json.tmp'); assert.equal(to, 'pids.json');
        if (blocked) throw Object.assign(new Error('simulated sharing violation'), { code: 'EPERM' });
        saved = staged;
      },
    },
    setTimeout(callback, delay) { assert.ok(delay > 0 && delay <= 1000); callbacks.push(callback); return callbacks.length; },
    clearTimeout() {},
  });
  assert.equal(save(), false);
  assert.deepEqual(JSON.parse(saved), { manager: 1, children: [] });
  current = { manager: 1, children: [{ name: 'agent-api', pid: 101 }, { name: 'agent-jobs', pid: 102 }] };
  assert.equal(save(), false);
  assert.equal(callbacks.length, 1, 'Only one retry can be pending');
  blocked = false;
  callbacks.shift()();
  assert.deepEqual(JSON.parse(saved), current);
  assert.ok(logs.some(line => line.includes('recovered')));
});

test('a write failure never reaches rename and can recover without stopping the manager', () => {
  let writes = 0, renames = 0;
  const retries = [];
  const save = createStateWriter('pids.json', () => ({ manager: 2 }), () => {}, {
    fs: {
      writeFileSync() { if (++writes === 1) throw Object.assign(new Error('busy'), { code: 'EBUSY' }); },
      renameSync() { renames++; },
    },
    setTimeout(callback) { retries.push(callback); return 1; }, clearTimeout() {},
  });
  assert.equal(save(), false); assert.equal(renames, 0);
  retries.shift()();
  assert.equal(renames, 1);
});
