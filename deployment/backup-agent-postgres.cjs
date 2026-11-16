// Encrypted, consistent PostgreSQL pre-migration snapshot. This is not a cross-store backup.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { execFileSync } = require('node:child_process');
const { Client } = require('./node_modules/ssh2');

const stamp = new Date().toISOString().replace(/[:.]/g, '-');
const directory = path.join(__dirname, '.run', 'pg-backups');
const keyDirectory = path.join(process.env.USERPROFILE, '.codex', 'newshop-backup-keys');
fs.mkdirSync(directory, { recursive: true });
fs.mkdirSync(keyDirectory, { recursive: true });
const keyPath = path.join(keyDirectory, `pg-${stamp}.key`);
const output = path.join(directory, `mall-agent-${stamp}.dump.enc`);
const key = crypto.randomBytes(32);
const iv = crypto.randomBytes(12);
fs.writeFileSync(keyPath, key, { flag: 'wx', mode: 0o600 });
if (process.platform === 'win32') execFileSync('icacls.exe', [keyPath, '/inheritance:r', '/grant:r', `${process.env.USERNAME}:(F)`], { windowsHide: true, stdio: 'ignore' });
const cipher = crypto.createCipheriv('aes-256-gcm', key, iv);
const plainHash = crypto.createHash('sha256');
const encryptedHash = crypto.createHash('sha256');
const destination = fs.createWriteStream(output, { flags: 'wx', mode: 0o600 });
const client = new Client();
let bytes = 0;
client.on('ready', () => client.exec('docker exec mall-agent-postgres pg_dump -U mall_agent_owner -d mall_agent --format=custom --no-owner --no-acl', (error, stream) => {
  if (error) { client.end(); throw new Error('PostgreSQL snapshot transport unavailable'); }
  stream.on('data', chunk => {
    bytes += chunk.length;
    plainHash.update(chunk);
    const encrypted = cipher.update(chunk);
    encryptedHash.update(encrypted);
    if (!destination.write(encrypted)) { stream.pause(); destination.once('drain', () => stream.resume()); }
  });
  stream.stderr.on('data', () => {});
  stream.on('close', code => {
    client.end();
    if (code || bytes < 100) { destination.destroy(); process.exitCode = 1; console.error('PostgreSQL snapshot failed; partial encrypted artifact retained for diagnosis'); return; }
    const final = cipher.final();
    encryptedHash.update(final);
    destination.end(final, () => {
      const manifest = {
        schema: 'encrypted-postgres-snapshot-v1', created_at: new Date().toISOString(), database: 'mall_agent',
        scope: 'POSTGRES_ONLY_CONSISTENT_PG_DUMP', includes_checkpoint_schema: true,
        artifact: output, key_file: keyPath, algorithm: 'AES-256-GCM', iv: iv.toString('hex'),
        authentication_tag: cipher.getAuthTag().toString('hex'), plaintext_sha256: plainHash.digest('hex'),
        ciphertext_sha256: encryptedHash.digest('hex'), plaintext_bytes: bytes, restore_verified: false,
      };
      fs.writeFileSync(output + '.json', JSON.stringify(manifest, null, 2));
      console.log('PASS encrypted PostgreSQL snapshot:', output + '.json');
    });
  });
}));
client.on('error', () => { destination.destroy(); console.error('PostgreSQL backup SSH unavailable'); process.exitCode = 1; });
client.connect(JSON.parse(fs.readFileSync(path.join(__dirname, '.dev-ssh.json'), 'utf8')));
