const fs = require('node:fs');

// Windows readers and antivirus software can briefly deny rename. Keep the last
// valid JSON and retry the latest state; a status file must not kill all services.
function createStateWriter(file, snapshot, log, dependencies = {}) {
  const disk = dependencies.fs || fs;
  const schedule = dependencies.setTimeout || setTimeout;
  const cancel = dependencies.clearTimeout || clearTimeout;
  let timer = null, failures = 0;
  function save() {
    try {
      disk.writeFileSync(file + '.tmp', JSON.stringify(snapshot(), null, 2));
      disk.renameSync(file + '.tmp', file);
      if (timer !== null) cancel(timer);
      timer = null;
      if (failures) log('Process state file recovered after ' + failures + ' failed writes');
      failures = 0;
      return true;
    } catch (error) {
      failures++;
      if (failures === 1 || failures % 30 === 0) {
        log('Process state file temporarily unavailable (' + (error.code || 'IO_ERROR') + '); retrying');
      }
      if (timer === null) {
        timer = schedule(() => { timer = null; save(); }, Math.min(1000, 50 * failures));
      }
      return false;
    }
  }
  return save;
}

module.exports = { createStateWriter };
