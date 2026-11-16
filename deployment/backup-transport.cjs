// Fixed backup/restore transport. Credentials and dump bytes never enter logs.
const fs = require('node:fs');
const path = require('node:path');
const net = require('node:net');
const { Client } = require('./node_modules/ssh2');
const operation = process.argv[2];
const target = process.argv[3];
const validId = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
const restore = operation?.startsWith('restore-');
if (restore && !validId.test(target || '')) throw new Error('Invalid registered restore identifier');
const prefix = restore ? 'mall-agent-r-' + target.replaceAll('-', '') : null;
const mysql = 'sh -c \'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec ';
const dumpOptions = '--single-transaction --skip-lock-tables --no-tablespaces --set-gtid-purged=OFF --column-statistics=0 --hex-blob --order-by-primary --skip-comments --skip-dump-date';
const commands = {
  'pg-dump': 'docker exec mall-agent-postgres pg_dump -U mall_agent_owner -d mall_agent --format=custom --no-owner --no-acl',
  'mysql-dump': 'docker exec mall-swarm-mysql-1 ' + mysql + 'mysqldump -uroot ' + dumpOptions + ' --routines --events --triggers mall\'',
  'mysql-data': 'docker exec mall-swarm-mysql-1 ' + mysql + 'mysqldump -uroot ' + dumpOptions + ' --compact --no-create-info --skip-add-locks --skip-disable-keys --skip-extended-insert mall\'',
  'mysql-lock': 'docker exec -i mall-swarm-mysql-1 ' + mysql + 'mysql -uroot --batch --skip-column-names --unbuffered\'',
  'mysql-state': 'docker exec mall-swarm-mysql-1 ' + mysql + 'mysql -uroot --batch --skip-column-names -e "SELECT @@event_scheduler; SELECT COUNT(*) FROM information_schema.events WHERE event_schema=0x6d616c6c; SHOW MASTER STATUS;"\'',
};
if (restore && operation === 'restore-pg') commands[operation] = `docker exec -i ${prefix}-pg pg_restore -U mall_agent_owner -d mall_agent --no-owner --no-acl --exit-on-error`;
if (restore && operation === 'restore-mysql') commands[operation] = `docker exec -i ${prefix}-mysql ` + mysql + 'mysql -uroot mall\'';
if (restore && operation === 'restore-mysql-data') commands[operation] = `docker exec ${prefix}-mysql ` + mysql + 'mysqldump -uroot ' + dumpOptions + ' --compact --no-create-info --skip-add-locks --skip-disable-keys --skip-extended-insert mall\'';
const remoteControl = restore && ['restore-create', 'restore-status', 'restore-destroy'].includes(operation);
if (!commands[operation] && !remoteControl && operation !== 'restore-tunnel') throw new Error('Unregistered backup transport operation');
const client = new Client();
const servers = [], sockets = new Set();
let failed = false;
function fail() { if (!failed) { failed = true; process.stderr.write('Registered backup transport unavailable\n'); } process.exitCode = 1; client.end(); }
client.on('ready', () => {
  if (operation === 'restore-tunnel') {
    const encoded = Buffer.from(JSON.stringify({ operation: 'restore-endpoints', plan_id: target })).toString('base64');
    client.exec('python3 -', (error, channel) => {
      if (error) return fail();
      let output = '';
      channel.on('data', chunk => { output += chunk.toString(); });
      channel.stderr.on('data', chunk => { process.stderr.write(chunk); });
      channel.once('close', code => {
        if (code) return fail();
        let response;
        try { response = JSON.parse(output); if (response.plan_id !== target) throw Error(); } catch { return fail(); }
        const ports = {};
        Promise.all(Object.entries(response.endpoints).map(([name, remote]) => new Promise((resolve, reject) => {
      if (!['pg','mysql','objects','vectors'].includes(name) || !/^(?:10|172|192)\.[0-9.]+$/.test(remote.host)
        || ![5432,3306,9000,19530].includes(remote.port)) return reject(Error('Invalid restore endpoint'));
      const server = net.createServer(socket => {
        sockets.add(socket); socket.once('close', () => sockets.delete(socket));
        client.forwardOut('127.0.0.1', socket.remotePort, remote.host, remote.port, (error, stream) => {
          if (error || socket.destroyed) { stream?.destroy(); socket.destroy(); return; }
          stream.on('error', () => socket.destroy()); socket.on('error', () => stream.destroy());
          stream.on('close', () => socket.destroy()); socket.on('close', () => stream.destroy());
          socket.pipe(stream).pipe(socket);
        });
      });
      server.once('error', reject); servers.push(server);
      server.listen(0, '127.0.0.1', () => { ports[name] = server.address().port; resolve(); });
        }))).then(() => {
      process.stdout.write(JSON.stringify({ ports, plan_id: target }) + '\n');
      process.stdin.resume();
      process.stdin.on('end', () => { sockets.forEach(socket => socket.destroy()); servers.forEach(server => server.close()); client.end(); });
        }).catch(fail);
      });
      channel.end('import json,base64\nrequest=json.loads(base64.b64decode("' + encoded + '"))\n' + fs.readFileSync(path.join(__dirname, 'backup-remote.py'), 'utf8'));
    });
    return;
  }
  const command = remoteControl ? 'python3 -' : commands[operation];
  client.exec(command, (error, stream) => {
    if (error) return fail();
    stream.on('data', chunk => { if (!process.stdout.write(chunk)) { stream.pause(); process.stdout.once('drain', () => stream.resume()); } });
    // Remote errors may contain sensitive SQL/objects. Return a stable error only.
    stream.stderr.on('data', () => {});
    stream.on('error', fail);
    stream.on('close', code => { client.end(); if (code !== 0) fail(); });
    if (operation === 'mysql-lock') {
      const timer = setTimeout(fail, 20000);
      stream.on('data', chunk => { if (chunk.toString().includes('AGENT_LOCKED')) clearTimeout(timer); });
      stream.write("SET SESSION lock_wait_timeout=10; FLUSH TABLES WITH READ LOCK; SELECT 'AGENT_LOCKED'; SHOW MASTER STATUS;\n");
      process.stdin.once('data', input => { if (input.toString().trim() === 'release') stream.end('UNLOCK TABLES;\n'); else fail(); });
      process.stdin.once('end', () => stream.end('UNLOCK TABLES;\n'));
    } else if (remoteControl) {
      const chunks = []; let length = 0;
      process.stdin.on('data', chunk => { length += chunk.length; if (length > 16384) return fail(); chunks.push(chunk); });
      process.stdin.on('end', () => {
        if (failed) return;
        const request = JSON.parse(Buffer.concat(chunks).toString() || '{}');
        request.operation = operation; request.plan_id = target;
        const encoded = Buffer.from(JSON.stringify(request)).toString('base64');
        stream.end('import json,base64\nrequest=json.loads(base64.b64decode("' + encoded + '"))\n' + fs.readFileSync(path.join(__dirname, 'backup-remote.py'), 'utf8'));
      });
      process.stdin.resume();
    } else if (['restore-pg', 'restore-mysql'].includes(operation)) process.stdin.pipe(stream);
    else stream.end();
  });
});
client.on('error', fail);
client.on('close', () => { if (operation === 'mysql-lock' || operation === 'restore-tunnel') process.stdin.destroy(); sockets.forEach(socket => socket.destroy()); servers.forEach(server => server.close()); });
client.connect({ ...JSON.parse(fs.readFileSync(path.join(__dirname, '.dev-ssh.json'), 'utf8')), readyTimeout: 20000, keepaliveInterval: 10000, keepaliveCountMax: 3 });
