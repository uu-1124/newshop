const assert = require('node:assert/strict');
const test = require('node:test');
const { maintenanceState, writerPaused, WRITERS } = require('../maintenance-state.cjs');

test('all application writers stay paused across delayed restarts and manager recreation', () => {
  const disk = { readFileSync: () => JSON.stringify({schema:'agent-maintenance-v1',plan_id:'12345678-1234-1234-1234-123456789abc',state:'PAUSED'}) };
  for (const name of WRITERS) assert.equal(writerPaused(name, 'maintenance.json', disk), true, name);
  assert.equal(writerPaused('admin-web', 'maintenance.json', disk), false);
  assert.equal(writerPaused('shop-web', 'maintenance.json', disk), false);
  assert.equal(writerPaused('agent-backups', 'maintenance.json', disk), false);
});

test('missing marker enables writers but unreadable or invalid marker fails closed', () => {
  const missing = {readFileSync(){throw Object.assign(new Error('missing'),{code:'ENOENT'});}};
  assert.equal(maintenanceState('maintenance.json', missing).active, false);
  for (const disk of [{readFileSync:()=>'{broken'}, {readFileSync:()=>'{"schema":"wrong"}'},
    {readFileSync(){throw Object.assign(new Error('sharing violation'),{code:'EPERM'});}}]) {
    assert.equal(writerPaused('portal', 'maintenance.json', disk), true);
  }
});
