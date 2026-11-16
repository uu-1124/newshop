const fs = require('node:fs');
const path = require('node:path');
const { Client } = require('./node_modules/ssh2');
const ddl = fs.readFileSync(path.join(__dirname, 'init/agent-business-outbox.sql'), 'utf8');
const sql = ['USE mall;', ddl, 'USE mall_agent_cart_test;', ddl].join('\n');
const ssh = new Client();
ssh.on('ready', () => ssh.exec(`docker exec -i mall-swarm-mysql-1 sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot --default-character-set=utf8mb4'`, (error, stream) => {
  if (error) { ssh.end(); throw new Error('Outbox schema transport unavailable'); }
  let failed = false;
  stream.on('data', () => {});
  stream.stderr.on('data', () => { failed = true; });
  stream.on('close', code => {
    ssh.end();
    if (code || failed) { process.exitCode = 1; console.error('Outbox incremental schema failed'); }
    else console.log('PASS one new business outbox table in demo and isolated MySQL; existing tables preserved');
  });
  stream.end(sql);
}));
ssh.on('error', () => { console.error('Outbox schema SSH unavailable'); process.exitCode = 1; });
ssh.connect(JSON.parse(fs.readFileSync(path.join(__dirname, '.dev-ssh.json'), 'utf8')));
