const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

test('gateway launches with UTF-8 while other services retain their JVM options', () => {
  const launches = [];
  const server = () => ({ on() { return this; }, listen() {}, close() {} });
  class Client {
    on(event, handler) { if (event === 'ready') this.ready = handler; return this; }
    connect() { this.ready(); }
  }
  const fakeFs = {
    mkdirSync() {}, writeFileSync() {}, renameSync() {}, closeSync() {}, openSync() { return 1; },
    existsSync() { return false; },
    readFileSync(file) {
      assert.equal(path.basename(file), '.dev-ssh.json');
      return '{}';
    },
  };
  const modules = {
    'node:fs': fakeFs, 'node:path': path,
    './process-state.cjs': { createStateWriter: (file, snapshot, log) => require('../process-state.cjs').createStateWriter(file, snapshot, log, { fs: fakeFs }) },
    './maintenance-state.cjs': { maintenanceState: () => ({active:false}), writerPaused: () => false },
    'node:net': { createServer: server }, 'node:http': { createServer: server },
    ssh2: { Client },
    'node:child_process': {
      spawn(exe, args, options) {
        launches.push({ exe, args, options });
        return { pid: 100 + launches.length, on() {} };
      },
    },
  };
  const source = fs.readFileSync(path.join(__dirname, '../dev.cjs'), 'utf8');
  vm.runInNewContext(source, {
    require(name) { assert.ok(name in modules, name); return modules[name]; },
    __dirname: path.resolve(__dirname, '..'),
    console: { log() {} },
    process: { pid: 42, execPath: process.execPath, env: { PATH: '' }, on() {}, exit() { assert.fail('Unexpected exit'); } },
  });
  const java = launches.filter(x => x.args.includes('-jar'));
  assert.equal(java.length, 6);
  for (const launch of java) {
    const jar = launch.args[launch.args.indexOf('-jar') + 1];
    if (path.basename(jar) === 'mall-gateway.jar') {
      assert.equal(launch.args.filter(x => x === '-Dfile.encoding=UTF-8').length, 1);
    } else {
      assert.equal(launch.args.some(x => x.startsWith('-Dfile.encoding=')), false);
    }
    assert.ok(launch.args.includes('-Duser.timezone=Asia/Shanghai'));
    assert.ok(launch.args.includes('--spring.profiles.active=deploy'));
  }
  assert.equal(launches.length, 8);
});
