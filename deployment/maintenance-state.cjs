const fs = require('node:fs');

const WRITERS = new Set(['monitor', 'auth', 'gateway', 'admin', 'portal', 'search',
  'agent-api', 'agent-generation', 'agent-jobs', 'agent-scheduler', 'agent-outbox','agent-observability']);

function maintenanceState(file, disk = fs) {
  try {
    const value = JSON.parse(disk.readFileSync(file, 'utf8'));
    if (value.schema !== 'agent-maintenance-v1' || !/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(value.plan_id)
      || !['PAUSING', 'PAUSED', 'RESUMING'].includes(value.state)) throw new Error('Invalid maintenance marker');
    return { active: true, plan_id: value.plan_id, state: value.state };
  } catch (error) {
    if (error.code === 'ENOENT') return { active: false };
    // A corrupt or temporarily unreadable marker cannot authorize writers.
    return { active: true, state: 'UNREADABLE' };
  }
}

function writerPaused(name, file, disk = fs) {
  return WRITERS.has(name) && maintenanceState(file, disk).active;
}

module.exports = { WRITERS, maintenanceState, writerPaused };
