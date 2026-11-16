const fs = require('node:fs'), path = require('node:path');
const { Client } = require('./node_modules/ssh2');
const tables = ['ums_member_receive_address','oms_order','oms_order_item','oms_order_setting',
  'ums_integration_consume_setting','sms_coupon','sms_coupon_history','sms_coupon_product_relation',
  'sms_coupon_product_category_relation','pms_product_ladder','pms_product_full_reduction'];
const additions = ['mall','mall_agent_cart_test'].map(database => `
SET @ddl = IF((SELECT COUNT(*) FROM information_schema.columns WHERE table_schema='${database}'
  AND table_name='oms_agent_quote' AND column_name='checkout_fingerprint')=0,
  'ALTER TABLE ${database}.oms_agent_quote ADD COLUMN checkout_fingerprint char(64) CHARACTER SET ascii COLLATE ascii_bin NULL', 'DO 0');
PREPARE order_schema FROM @ddl; EXECUTE order_schema; DEALLOCATE PREPARE order_schema;`).join('\n');
const sql = `${additions}\n${tables.map(t=>`CREATE TABLE IF NOT EXISTS mall_agent_cart_test.${t} LIKE mall.${t};`).join('\n')}`;
const ssh = new Client();
ssh.on('ready',()=>ssh.exec(`docker exec -i mall-swarm-mysql-1 sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot --default-character-set=utf8mb4'`,(err,stream)=>{
  if(err){console.error('Order schema transport unavailable');ssh.end();process.exitCode=1;return;}
  stream.on('data',()=>{});let failed=false;
  stream.stderr.on('data',()=>{failed=true;});
  stream.on('close',code=>{ssh.end();if(code||failed){console.error('Order schema update failed');process.exitCode=1;}else console.log('PASS order quote column and isolated checkout test tables prepared');});
  stream.end(sql);
}));
ssh.on('error',()=>{console.error('Order schema SSH unavailable');process.exitCode=1;});
ssh.connect(JSON.parse(fs.readFileSync(path.join(__dirname,'.dev-ssh.json'),'utf8')));
