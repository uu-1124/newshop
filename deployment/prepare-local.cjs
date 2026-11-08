const fs=require('node:fs'),path=require('node:path');
const root=__dirname;
const secrets=JSON.parse(fs.readFileSync(path.join(root,'secrets.json')));
const compose=JSON.parse(fs.readFileSync(path.join(root,'compose.yaml')));
const middleware=['mysql','redis','mongo','rabbitmq','nacos','elasticsearch','minio'];
compose.services=Object.fromEntries(middleware.map(x=>[x,compose.services[x]]));
const mappings={mysql:['3306:3306'],redis:['6379:6379'],mongo:['27017:27017'],rabbitmq:['5672:5672','15672:15672'],nacos:['8848:8848','9848:9848'],elasticsearch:['9200:9200'],minio:['9000:9000','9001:9001']};
for(const [name,ports] of Object.entries(mappings))compose.services[name].ports=ports.map(p=>'127.0.0.1:'+p);
delete compose.services.minio.networks;
fs.writeFileSync(path.join(root,'compose.yaml'),JSON.stringify(compose,null,2)+'\n');
const ports={admin:8080,portal:8085,search:8081,auth:8401,gateway:8201,monitor:8101};
for(const [name,port] of Object.entries(ports)){
 const cfg={server:{address:'127.0.0.1'},spring:{profiles:{active:'deploy'},cloud:{nacos:{discovery:{'server-addr':'127.0.0.1:18848',ip:'127.0.0.1'},config:{enabled:false,'import-check':{enabled:false}}}},boot:{admin:{client:{url:'http://127.0.0.1:8101',username:'macro',password:'123456',instance:{'service-base-url':'http://127.0.0.1:'+port}}}},datasource:{url:'jdbc:mysql://127.0.0.1:13306/mall?useUnicode=true&characterEncoding=utf-8&serverTimezone=Asia/Shanghai&useSSL=false&allowPublicKeyRetrieval=true',username:'mall',password:secrets.mysql,druid:{'initial-size':2,'min-idle':2,'max-active':15}},data:{redis:{host:'127.0.0.1',port:16379,password:secrets.redis},mongodb:{host:'127.0.0.1',port:27018,database:'mall-port'}},rabbitmq:{host:'127.0.0.1',port:15672,'virtual-host':'/mall',username:'mall',password:secrets.rabbit},elasticsearch:{uris:'http://127.0.0.1:19200'}},'sa-token':{'jwt-secret-key':secrets.jwt},management:{endpoints:{web:{exposure:{include:'health,info'}}},endpoint:{health:{'show-details':'never'}}},logstash:{enableInnerLog:false},minio:{endpoint:'http://127.0.0.1:19000',bucketName:'mall',accessKey:'mallminio',secretKey:secrets.minio},logging:{level:{root:'INFO','com.macro.mall':'INFO','org.mongodb.driver':'WARN','de.codecentric.boot.admin.client':'INFO'}}};
 fs.mkdirSync(path.join(root,'config'),{recursive:true});
 fs.writeFileSync(path.join(root,'config',name+'.yaml'),JSON.stringify(cfg,null,2)+'\n');
}
for(const env of ['development','production']){
 fs.writeFileSync(path.join(root,'../mall-admin-web/.env.'+env+'.local'),'VITE_BASE_SERVER_URL=http://127.0.0.1:8201/mall-admin\nVITE_USE_OSS=false\n');
 fs.writeFileSync(path.join(root,'../mall-app-web/.env.'+env+'.local'),'VITE_API_BASE_URL=http://127.0.0.1:8201/mall-portal\nVITE_USE_ALIPAY=false\n');
}
console.log('Configured local Java/frontends and server middleware; no public middleware ports.');
