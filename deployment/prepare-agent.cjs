// Additive provisioning: dedicated PostgreSQL, database and volume. Existing mall services are untouched.
const fs = require('node:fs'), path = require('node:path'), crypto = require('node:crypto');
const { Client } = require('ssh2');
const root = __dirname, run = path.join(root, '.run');
fs.mkdirSync(run, { recursive: true });
const configPath = path.join(run, 'agent-env.json');
let config;
if (fs.existsSync(configPath)) config = JSON.parse(fs.readFileSync(configPath));
else {
  const random = () => crypto.randomBytes(32).toString('hex');
  const owner = random(), runtime = random();
  config = {
    databaseUrl: `postgresql+psycopg://mall_agent:${runtime}@127.0.0.1:15432/mall_agent`,
    migrationUrl: `postgresql+psycopg://mall_agent_owner:${owner}@127.0.0.1:15432/mall_agent`,
    runtimePassword: runtime, ownerPassword: owner,
    bridgeSecret: random(), brokerSecret: random(), cursorSecret: random(), enabled: false,
  };
  fs.writeFileSync(configPath, JSON.stringify(config, null, 2), { mode: 0o600 });
}
const ssh = JSON.parse(fs.readFileSync(path.join(root, '.dev-ssh.json')));
if (ssh.host !== '113.249.103.102') throw new Error('Unexpected server');
const compose = `name: mall-agent
services:
  postgres:
    image: postgres:16-alpine
    container_name: mall-agent-postgres
    restart: unless-stopped
    mem_limit: 1g
    cpus: 1.5
    env_file: .env
    ports: ["127.0.0.1:15432:5432"]
    volumes: ["mall-agent-pgdata:/var/lib/postgresql/data"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U mall_agent_owner -d mall_agent"]
      interval: 5s
      timeout: 3s
      retries: 12
    logging:
      driver: local
      options: {max-size: "10m", max-file: "3"}
volumes:
  mall-agent-pgdata:
`;
const remoteEnv = `POSTGRES_USER=mall_agent_owner\nPOSTGRES_DB=mall_agent\nPOSTGRES_PASSWORD=${config.ownerPassword}\n`;
const script = `import os,subprocess,time
base='/opt/mall-agent'
os.makedirs(base,mode=0o700,exist_ok=True)
files=${JSON.stringify({ 'compose.yaml': Buffer.from(compose).toString('base64'), '.env': Buffer.from(remoteEnv).toString('base64') })}
import base64
for name,content in files.items():
 p=os.path.join(base,name)
 data=base64.b64decode(content)
 if os.path.exists(p):
  if open(p,'rb').read()!=data: raise SystemExit('Existing agent configuration differs; left untouched')
 else:
  fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
  with os.fdopen(fd,'wb') as f:f.write(data)
subprocess.check_call(['docker','compose','-f',base+'/compose.yaml','up','-d'])
for _ in range(30):
 r=subprocess.run(['docker','inspect','-f','{{.State.Health.Status}}','mall-agent-postgres'],stdout=subprocess.PIPE,universal_newlines=True)
 if r.stdout.strip()=='healthy':break
 time.sleep(1)
else:raise SystemExit('PostgreSQL readiness timeout')
sql="DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='mall_agent') THEN CREATE ROLE mall_agent LOGIN PASSWORD '${config.runtimePassword}'; END IF; END $$;"
subprocess.run(['docker','exec','-i','mall-agent-postgres','psql','-U','mall_agent_owner','-d','mall_agent','-v','ON_ERROR_STOP=1'],input=sql,universal_newlines=True,check=True)
print('Dedicated PostgreSQL ready; existing mall database untouched')
`;
const client = new Client();
client.on('ready', () => client.exec('python3 -', (err, stream) => {
  if (err) { client.end(); throw new Error('SSH provisioning failed'); }
  stream.on('data', data => process.stdout.write(data));
  stream.stderr.on('data', data => process.stderr.write(data));
  stream.on('close', code => {
    client.end();
    if (code === 0) fs.writeFileSync(configPath, JSON.stringify({ ...config, postgresMode: 'cloud' }, null, 2), { mode: 0o600 });
    process.exitCode = code;
  });
  stream.end(script);
}));
client.on('error', () => { console.error('SSH connection failed'); process.exitCode = 1; });
client.connect({ ...ssh, readyTimeout: 25000 });
