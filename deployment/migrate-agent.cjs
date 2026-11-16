const fs = require('node:fs'), net = require('node:net'), path = require('node:path');
const { spawn } = require('node:child_process');
const { Client } = require('ssh2');
const config = JSON.parse(fs.readFileSync(path.join(__dirname, '.run', 'agent-env.json')));
const ssh = JSON.parse(fs.readFileSync(path.join(__dirname, '.dev-ssh.json')));
const agent = path.resolve(__dirname, '../mall-agent');
const sockets = new Set();
const client = new Client();
const tunnel = net.createServer(socket => {
  sockets.add(socket);
  socket.on('error', () => socket.destroy());
  socket.on('close', () => sockets.delete(socket));
  client.forwardOut('127.0.0.1', socket.remotePort, '127.0.0.1', 15432, (err, stream) => {
    if (err || socket.destroyed) { stream?.destroy(); return socket.destroy(); }
    stream.on('error', () => socket.destroy());
    socket.on('close', () => stream.destroy());
    stream.pipe(socket).pipe(stream);
  });
});
function finish(code) {
  for (const socket of sockets) socket.destroy();
  tunnel.close(); client.end(); process.exitCode = code;
}
tunnel.on('error', () => { console.error('Migration tunnel port 15432 unavailable; stop the demo manager first.'); finish(1); });
client.on('error', () => { console.error('SSH migration tunnel unavailable.'); finish(1); });
client.on('ready', () => tunnel.listen(15432, '127.0.0.1', () => {
  const child = spawn(path.join(agent, '.venv', 'Scripts', 'python.exe'), ['scripts/migrate.py'],
    { cwd: agent, stdio: 'inherit', windowsHide: true });
  child.on('exit', code => finish(code ?? 1));
  child.on('error', () => finish(1));
}));
if (config.postgresMode !== 'cloud') throw new Error('Prepare the isolated cloud PostgreSQL before migration');
client.connect({ ...ssh, readyTimeout: 25000 });
