const fs = require('node:fs'), path = require('node:path'), crypto = require('node:crypto');
const { Client } = require('./node_modules/ssh2');
const root = __dirname, privatePath = path.join(root, '.run', 'cart-test-env.json');
const test = fs.existsSync(privatePath) ? JSON.parse(fs.readFileSync(privatePath, 'utf8')) : {
  database: 'mall_agent_cart_test', user: 'mall_agent_cart_test', password: crypto.randomBytes(32).toString('hex'),
};
if (test.database !== 'mall_agent_cart_test' || test.user !== 'mall_agent_cart_test' || !/^[a-f0-9]{64}$/.test(test.password)) throw new Error('Unexpected test database identity');
const schema = fs.readFileSync(path.join(root, 'init', 'agent-cart.sql'), 'utf8');
const tables = ['pms_product', 'pms_sku_stock', 'oms_cart_item', 'ums_member'];
const sql = `USE mall;
${schema}
CREATE DATABASE IF NOT EXISTS mall_agent_cart_test CHARACTER SET utf8mb4;
${tables.map(t => `CREATE TABLE IF NOT EXISTS mall_agent_cart_test.${t} LIKE mall.${t};`).join('\n')}
USE mall_agent_cart_test;
${schema}
INSERT IGNORE INTO pms_product SELECT p.* FROM mall.pms_product p WHERE p.id IN
 (SELECT product_id FROM mall.pms_sku_stock WHERE price>=0 AND stock-COALESCE(lock_stock,0)>5) AND p.publish_status=1 AND p.delete_status=0 AND p.product_category_id IS NOT NULL ORDER BY p.id LIMIT 2;
INSERT IGNORE INTO pms_sku_stock SELECT s.* FROM mall.pms_sku_stock s JOIN pms_product p ON p.id=s.product_id WHERE s.price>=0 AND s.stock-COALESCE(s.lock_stock,0)>5;
CREATE USER IF NOT EXISTS '${test.user}'@'%' IDENTIFIED BY '${test.password}';
ALTER USER '${test.user}'@'%' IDENTIFIED BY '${test.password}';
GRANT SELECT,INSERT,UPDATE,DELETE,CREATE,ALTER,INDEX ON mall_agent_cart_test.* TO '${test.user}'@'%';
`;
const ssh = new Client();
ssh.on('ready', () => ssh.exec(`docker exec -i mall-swarm-mysql-1 sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot --default-character-set=utf8mb4'`, (err, stream) => {
  if (err) { console.error('Cart schema transport unavailable'); ssh.end(); process.exitCode = 1; return; }
  let error = '';
  stream.on('data', () => {});
  stream.stderr.on('data', chunk => { error += chunk.toString(); });
  stream.on('close', code => {
    ssh.end();
    if (code) { console.error('Cart schema rejected; database changes require review', error.replace(/IDENTIFIED BY[^;]+/g, 'IDENTIFIED BY [redacted]')); process.exitCode = 1; return; }
    fs.writeFileSync(privatePath, JSON.stringify(test, null, 2));
    console.log('PASS scoped cart schema and dedicated MySQL test user prepared');
  });
  stream.end(sql);
}));
ssh.on('error', () => { console.error('Cart schema SSH unavailable'); process.exitCode = 1; });
ssh.connect(JSON.parse(fs.readFileSync(path.join(root, '.dev-ssh.json'), 'utf8')));
