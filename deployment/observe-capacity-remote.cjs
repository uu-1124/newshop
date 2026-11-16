// Fixed aggregate CPU observation only; no caller-controlled command or destination.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { Client } = require('./node_modules/ssh2');
const client = new Client();
const started = Date.now();
const timer = setTimeout(() => { client.end(); process.exitCode = 1; }, 20000);
const source = `import json,os
line=open('/proc/stat').readline().split()
values=list(map(int,line[1:9]))
print(json.dumps({'ticks':dict(zip(['user','nice','system','idle','iowait','irq','softirq','steal'],values)), 'cpu_count':os.cpu_count(), 'load_average':list(os.getloadavg())}))
`;
client.on('ready', () => client.exec('python3 -', (error, stream) => {
  if (error) { clearTimeout(timer); client.end(); process.exitCode = 1; return; }
  let output = '';
  stream.on('data', data => {
    output += data.toString();
    if (output.length > 8192) { stream.destroy(); client.end(); process.exitCode = 1; }
  });
  stream.stderr.on('data', () => {});
  stream.on('close', code => {
    clearTimeout(timer); client.end();
    if (code) { process.exitCode = 1; return; }
    try {
      const cpu = JSON.parse(output);
      const ticks = {};
      for (const item of os.cpus()) for (const [key,value] of Object.entries(item.times)) ticks[key] = (ticks[key] || 0) + value;
      process.stdout.write(JSON.stringify({ cloud_cpu: cpu, local_cpu: { ticks, cpu_count: os.cpus().length }, local_memory: { total_bytes: os.totalmem(), free_bytes: os.freemem() }, ssh_observation_ms: Date.now() - started }) + '\n');
    } catch { process.stderr.write('Capacity CPU observation invalid\n'); process.exitCode = 1; }
  });
  stream.end(source);
}));
client.on('error', () => { clearTimeout(timer); process.stderr.write('Capacity CPU observation unavailable\n'); client.end(); process.exitCode = 1; });
client.connect({ ...JSON.parse(fs.readFileSync(path.join(__dirname,'.dev-ssh.json'),'utf8')), readyTimeout: 15000 });
