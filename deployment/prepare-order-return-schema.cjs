const fs=require('node:fs'),path=require('node:path');
const {Client}=require('./node_modules/ssh2');
const schema=fs.readFileSync(path.join(__dirname,'init/agent-order-return.sql'),'utf8');
const outbox=fs.readFileSync(path.join(__dirname,'init/agent-business-outbox.sql'),'utf8');
const rules=fs.readFileSync(path.join(__dirname,'init/agent-after-sale-rules.sql'),'utf8');
const tables=['oms_order_return_apply','oms_order_return_reason','oms_company_address'];
const sql=['USE mall;',schema,outbox,rules,'USE mall_agent_cart_test;',...tables.map(t=>`CREATE TABLE IF NOT EXISTS ${t} LIKE mall.${t};`),schema,outbox,rules].join('\n');
const ssh=new Client();
ssh.on('ready',()=>ssh.exec(`docker exec -i mall-swarm-mysql-1 sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot --default-character-set=utf8mb4'`,(err,stream)=>{
 if(err){console.error('Demo lifecycle schema transport unavailable');ssh.end();process.exitCode=1;return;}
 let failed=false;stream.on('data',()=>{});stream.stderr.on('data',()=>{failed=true;});
 stream.on('close',code=>{ssh.end();if(code||failed){console.error('Demo lifecycle schema update failed');process.exitCode=1;}else console.log('PASS incremental demo lifecycle schema and isolated test tables prepared');});stream.end(sql);
}));
ssh.on('error',()=>{console.error('Demo lifecycle schema SSH unavailable');process.exitCode=1;});
ssh.connect(JSON.parse(fs.readFileSync(path.join(__dirname,'.dev-ssh.json'),'utf8')));
