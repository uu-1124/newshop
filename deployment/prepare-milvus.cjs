// Isolated Milvus stack; no existing mall storage, networks or containers are changed.
const fs = require('node:fs'), path = require('node:path'), crypto = require('node:crypto');
const { Client } = require('ssh2');
const run = path.join(__dirname, '.run'), configPath = path.join(run, 'milvus-env.json');
const config = fs.existsSync(configPath) ? JSON.parse(fs.readFileSync(configPath)) : {
  rootPassword: crypto.randomBytes(24).toString('hex'),
  minioAccess: 'agent' + crypto.randomBytes(8).toString('hex'),
  minioSecret: crypto.randomBytes(24).toString('hex'),
};
fs.writeFileSync(configPath, JSON.stringify(config, null, 2));
const compose = `name: mall-agent-vector
services:
  etcd:
    image: quay.io/coreos/etcd:v3.5.25
    container_name: mall-agent-etcd
    restart: unless-stopped
    mem_limit: 384m
    cpus: 0.5
    environment:
      ETCD_AUTO_COMPACTION_MODE: revision
      ETCD_AUTO_COMPACTION_RETENTION: '1000'
      ETCD_QUOTA_BACKEND_BYTES: '4294967296'
      ETCD_SNAPSHOT_COUNT: '50000'
    command: etcd -advertise-client-urls=http://etcd:2379 -listen-client-urls http://0.0.0.0:2379 --data-dir /etcd
    volumes: [etcd-data:/etcd]
    healthcheck:
      test: [CMD, etcdctl, endpoint, health]
      interval: 10s
      timeout: 5s
      retries: 12
    logging: {driver: local, options: {max-size: 10m, max-file: '3'}}
  minio:
    image: milvusdb/minio:RELEASE.2024-12-18T13-15-44Z
    container_name: mall-agent-vector-minio
    restart: unless-stopped
    mem_limit: 512m
    cpus: 0.5
    environment:
      MINIO_ROOT_USER: \${MINIO_ACCESS}
      MINIO_ROOT_PASSWORD: \${MINIO_SECRET}
    command: minio server /data --console-address :9001
    volumes: [object-data:/data]
    healthcheck:
      test: [CMD, mc, ready, local]
      interval: 10s
      timeout: 5s
      retries: 12
    logging: {driver: local, options: {max-size: 10m, max-file: '3'}}
  volume-init:
    image: milvusdb/milvus:v3.0.5
    user: '0:0'
    network_mode: none
    read_only: true
    restart: 'no'
    entrypoint: [/bin/sh, -c]
    command: ['chown -R $$(id -u milvus):$$(id -g milvus) /var/lib/milvus']
    volumes: [milvus-data:/var/lib/milvus]
  milvus:
    image: milvusdb/milvus:v3.0.5
    container_name: mall-agent-milvus
    restart: unless-stopped
    mem_limit: 4g
    cpus: 2
    security_opt: [seccomp:unconfined]
    command: [milvus, run, standalone]
    environment:
      ETCD_ENDPOINTS: etcd:2379
      MINIO_ADDRESS: minio:9000
      MINIO_ACCESS_KEY_ID: \${MINIO_ACCESS}
      MINIO_SECRET_ACCESS_KEY: \${MINIO_SECRET}
      MINIO_REGION: us-east-1
      MQ_TYPE: woodpecker
      COMMON_SECURITY_AUTHORIZATIONENABLED: 'true'
    volumes:
      - milvus-data:/var/lib/milvus
      - ./user.yaml:/milvus/configs/user.yaml:ro
    ports: ['127.0.0.1:19530:19530', '127.0.0.1:19091:9091']
    depends_on:
      volume-init: {condition: service_completed_successfully}
      etcd: {condition: service_healthy}
      minio: {condition: service_healthy}
    healthcheck:
      test: [CMD, curl, -f, http://localhost:9091/healthz]
      interval: 10s
      start_period: 90s
      timeout: 5s
      retries: 18
    logging: {driver: local, options: {max-size: 10m, max-file: '3'}}
volumes:
  etcd-data:
  object-data:
  milvus-data:
`;
const files = {
  'compose.yaml': compose,
  '.env': `MINIO_ACCESS=${config.minioAccess}\nMINIO_SECRET=${config.minioSecret}\n`,
  'user.yaml': 'common:\n  security:\n    authorizationEnabled: true\netcd:\n  rootPath: mall-agent-v1\nminio:\n  rootPath: mall-agent-v1\nmq:\n  type: woodpecker\n',
};
const payload = Buffer.from(JSON.stringify(files)).toString('base64');
const script = `import base64,json,os,subprocess,shutil,concurrent.futures
base='/opt/mall-agent-vector'
available=int(next(x.split()[1] for x in open('/proc/meminfo') if x.startswith('MemAvailable:')))*1024
free=shutil.disk_usage('/opt').free
print('Resource preflight: available memory %.1f GiB; disk %.1f GiB'%(available/2**30,free/2**30),flush=True)
if available<6*2**30 or free<10*2**30:raise SystemExit('Insufficient headroom for isolated vector stack')
os.makedirs(base,mode=0o700,exist_ok=True)
for name,content in json.loads(base64.b64decode('${payload}').decode()).items():
 p=os.path.join(base,name)
 if os.path.exists(p) and open(p).read()!=content:raise SystemExit('Existing vector configuration differs; left untouched')
 if not os.path.exists(p):
  fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
  with os.fdopen(fd,'w') as f:f.write(content)
 if name=='user.yaml':os.chmod(p,0o644)
images=['quay.io/coreos/etcd:v3.5.25','milvusdb/minio:RELEASE.2024-12-18T13-15-44Z','milvusdb/milvus:v3.0.5']
def pull(image):
 if subprocess.call(['docker','image','inspect',image],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)==0:return
 mirror='docker.m.daocloud.io/'+image if not image.startswith('quay.io/') else image
 print('Pulling '+mirror,flush=True)
 subprocess.run(['docker','pull',mirror],check=True,timeout=240)
 if mirror!=image:subprocess.run(['docker','tag',mirror,image],check=True)
with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:list(pool.map(pull,images))
subprocess.run(['docker','compose','-f',base+'/compose.yaml','up','-d'],check=True)
print('Isolated vector stack started; health and authenticated data operations must still be verified',flush=True)
`;
const ssh = JSON.parse(fs.readFileSync(path.join(__dirname, '.dev-ssh.json')));
if (ssh.host !== '113.249.103.102') throw new Error('Unexpected server');
const client = new Client();
client.on('ready', () => client.exec('python3 -', (err, stream) => {
  if (err) throw new Error('Vector provisioning SSH failed');
  stream.on('data', b => process.stdout.write(b));
  stream.stderr.on('data', b => process.stderr.write(b));
  stream.on('close', code => { client.end(); process.exitCode = code; });
  stream.end(script);
}));
client.on('error', () => { console.error('Vector provisioning SSH unavailable'); process.exitCode = 1; });
client.connect({ ...ssh, readyTimeout: 25000, keepaliveInterval: 10000 });
