const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const out=__dirname;
const yaml={stringify:value=>JSON.stringify(value,null,2)+'\n'};
const secretPath=path.join(out,'secrets.json');
const random=()=>crypto.randomBytes(24).toString('hex');
const secrets=fs.existsSync(secretPath)?JSON.parse(fs.readFileSync(secretPath)): {mysql:random(),redis:random(),rabbit:random(),minio:random(),jwt:random(),nacos:crypto.randomBytes(48).toString('base64'),monitor:random(),demoPassword:'MallDemo2026!'};
secrets.mysqlRoot ||= random();
secrets.nacosIdentity ||= random();
for(const dir of ['config','init','jars','web/admin','web/shop','web/demo-assets'])fs.mkdirSync(path.join(out,dir),{recursive:true});
fs.writeFileSync(secretPath,JSON.stringify(secrets,null,2));
const write=(name,data)=>fs.writeFileSync(path.join(out,name),data);
const mirror='docker.m.daocloud.io/';
const services={
 mysql:{image:mirror+'library/mysql:8.0.35',restart:'unless-stopped',environment:{MYSQL_DATABASE:'mall',MYSQL_USER:'mall',MYSQL_PASSWORD:secrets.mysql,MYSQL_ROOT_PASSWORD:secrets.mysqlRoot,TZ:'Asia/Shanghai'},command:['--character-set-server=utf8mb4','--collation-server=utf8mb4_unicode_ci'],volumes:['mysql-data:/var/lib/mysql','./init:/docker-entrypoint-initdb.d:ro'],healthcheck:{test:['CMD-SHELL','MYSQL_PWD=$$MYSQL_ROOT_PASSWORD mysql -uroot -Nse "SELECT 1" >/dev/null'],interval:'10s',timeout:'5s',retries:30},mem_limit:'1500m'},
 redis:{image:mirror+'library/redis:7.4-alpine',restart:'unless-stopped',command:['redis-server','--appendonly','yes','--requirepass',secrets.redis],volumes:['redis-data:/data'],healthcheck:{test:['CMD','redis-cli','-a',secrets.redis,'ping'],interval:'10s',timeout:'5s',retries:20},mem_limit:'256m'},
 mongo:{image:mirror+'library/mongo:7.0',restart:'unless-stopped',volumes:['mongo-data:/data/db'],mem_limit:'768m'},
 rabbitmq:{image:mirror+'library/rabbitmq:3.13-management-alpine',restart:'unless-stopped',environment:{RABBITMQ_DEFAULT_USER:'mall',RABBITMQ_DEFAULT_PASS:secrets.rabbit,RABBITMQ_DEFAULT_VHOST:'/mall'},volumes:['rabbit-data:/var/lib/rabbitmq'],healthcheck:{test:['CMD','rabbitmq-diagnostics','-q','ping'],interval:'15s',timeout:'10s',retries:30},mem_limit:'768m'},
 nacos:{image:mirror+'nacos/nacos-server:v2.5.1',restart:'unless-stopped',environment:{MODE:'standalone',PREFER_HOST_MODE:'hostname',NACOS_AUTH_ENABLE:'false',NACOS_AUTH_TOKEN:secrets.nacos,NACOS_AUTH_IDENTITY_KEY:'mallserver',NACOS_AUTH_IDENTITY_VALUE:secrets.nacosIdentity,JVM_XMS:'256m',JVM_XMX:'512m',JVM_XMN:'128m'},volumes:['nacos-data:/home/nacos/data'],healthcheck:{test:['CMD-SHELL','curl -fsS http://localhost:8848/nacos/v1/console/health/readiness'],interval:'10s',timeout:'5s',retries:45},mem_limit:'1g'},
 elasticsearch:{image:mirror+'library/elasticsearch:8.18.0',restart:'unless-stopped',environment:{'discovery.type':'single-node','xpack.security.enabled':'false','xpack.security.enrollment.enabled':'false',ES_JAVA_OPTS:'-Xms512m -Xmx512m'},volumes:['es-data:/usr/share/elasticsearch/data','./es-ik:/usr/share/elasticsearch/plugins/ik:ro'],healthcheck:{test:['CMD-SHELL','curl -fsS http://localhost:9200/_cluster/health'],interval:'10s',timeout:'5s',retries:45},mem_limit:'1800m'},
 minio:{image:'mall-swarm/minio:RELEASE.2025-07-23T15-54-02Z',build:{context:'./minio-runtime'},restart:'unless-stopped',command:['server','/data','--console-address',':9001'],environment:{MINIO_ROOT_USER:'mallminio',MINIO_ROOT_PASSWORD:secrets.minio},ports:['127.0.0.1:9000:9000','127.0.0.1:9001:9001'],volumes:['minio-data:/data'],mem_limit:'512m'}
};
write('compose.yaml',yaml.stringify({name:'mall-swarm',services,volumes:Object.fromEntries(['mysql','redis','mongo','rabbit','nacos','es','minio'].map(x=>[x+'-data',{}]))}));
const hash='$2a$12$ydxx5bcbDhe/b5c2IDeequ8tj.qBN0BHcNrMUL1Q9JdbxjHyDKzUK';
write('init/02-demo-accounts.sql',`UPDATE ums_admin SET password='${hash}' WHERE username='admin';\nUPDATE ums_member SET password='${hash}' WHERE username='member';\nINSERT INTO ums_member(member_level_id,username,password,nickname,phone,status,create_time,gender,integration,growth) SELECT 1,'member','${hash}','演示买家','13800138000',1,NOW(),0,10000,10000 WHERE NOT EXISTS(SELECT 1 FROM ums_member WHERE username='member');\n`);
console.log('Deployment configuration generated; credentials saved locally.');

require('./prepare-local.cjs');
