// Verify authenticated decryption and pg_restore's archive catalogue, without restoring.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { Client } = require('./node_modules/ssh2');
const folder = path.join(__dirname, '.run', 'pg-backups');
const name = fs.readdirSync(folder).filter(n => n.endsWith('.dump.enc.json')).sort().at(-1);
if (!name) throw new Error('No encrypted PostgreSQL backup manifest');
const manifestPath = path.join(folder, name);
const manifest = JSON.parse(fs.readFileSync(manifestPath));
const encrypted = fs.readFileSync(manifest.artifact);
if (crypto.createHash('sha256').update(encrypted).digest('hex') !== manifest.ciphertext_sha256) throw new Error('Encrypted snapshot hash mismatch');
const decipher = crypto.createDecipheriv('aes-256-gcm', fs.readFileSync(manifest.key_file), Buffer.from(manifest.iv, 'hex'));
decipher.setAuthTag(Buffer.from(manifest.authentication_tag, 'hex'));
const plain = Buffer.concat([decipher.update(encrypted), decipher.final()]);
if (crypto.createHash('sha256').update(plain).digest('hex') !== manifest.plaintext_sha256) throw new Error('Decrypted snapshot hash mismatch');
const client = new Client();
client.on('ready', () => client.exec('docker exec -i mall-agent-postgres pg_restore --list', (error, stream) => {
  if (error) throw new Error('Archive verifier transport unavailable');
  let catalogue = '';
  stream.on('data', chunk => { catalogue += chunk.toString(); });
  stream.stderr.on('data', () => {});
  stream.on('close', code => {
    client.end();
    plain.fill(0);
    if (code || !catalogue.includes('langgraph_checkpoint') || !catalogue.includes('ag_conversation')) throw new Error('PostgreSQL archive catalogue invalid');
    manifest.authenticated_archive_verified = true;
    manifest.catalogue_entries = catalogue.split('\n').filter(line => /^\d+;/.test(line)).length;
    manifest.archive_verified_at = new Date().toISOString();
    fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2));
    console.log('PASS authenticated PostgreSQL archive and checkpoint catalogue; restore not executed');
  });
  stream.end(plain);
}));
client.on('error', () => { console.error('Backup verifier SSH unavailable'); process.exitCode = 1; });
client.connect(JSON.parse(fs.readFileSync(path.join(__dirname, '.dev-ssh.json'), 'utf8')));
