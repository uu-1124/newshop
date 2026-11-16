const fs=require('node:fs'),path=require('node:path'),net=require('node:net'),http=require('node:http');
const {spawn}=require('node:child_process');
const {Client}=require('ssh2');
const {createStateWriter}=require('./process-state.cjs');
const {maintenanceState,writerPaused}=require('./maintenance-state.cjs');
const root=__dirname,workspace=path.resolve(root,'..'),run=path.join(root,'.run');
fs.mkdirSync(run,{recursive:true});
const sshConfig=JSON.parse(fs.readFileSync(path.join(root,'.dev-ssh.json')));
const agentConfigPath=path.join(run,'agent-env.json');
const agentConfig=fs.existsSync(agentConfigPath)?JSON.parse(fs.readFileSync(agentConfigPath)):null;
const runtimePath=path.join(run,'runtime-env.json');
const runtimeConfig=fs.existsSync(runtimePath)?JSON.parse(fs.readFileSync(runtimePath)):null;
const forwards=[[13306,3306],[16379,6379],[27018,27017],[15672,5672],[15673,15672],[18848,8848],[19848,9848],[19200,9200],[19000,9000],[19001,9001]];
if(agentConfig?.postgresMode==='cloud')forwards.push([15432,15432]);
if(fs.existsSync(path.join(run,'milvus-env.json')))forwards.push([19530,19530],[19091,19091]);
const children=[],servers=[];let client,connected=false,started=false,closing=false;
const log=s=>console.log(new Date().toISOString()+' '+s);
const save=createStateWriter(path.join(run,'pids.json'),()=>({manager:process.pid,startedAt:new Date().toISOString(),children}),log);
function startChild(name,exe,args,cwd,extraEnv={}){
 const entry={name,pid:null,restarts:0};children.push(entry);
 let failures=0;
 function launch(){
  if(closing)return;
  if(writerPaused(name,path.join(run,'maintenance.json'))){
   entry.pid=null;entry.maintenance=true;save();setTimeout(launch,1000);return;
  }
  entry.maintenance=false;
  const fd=fs.openSync(path.join(run,name+'.log'),'a');
  let child;
  try{child=spawn(exe,args,{cwd,windowsHide:true,stdio:['ignore',fd,fd],env:{...process.env,PATH:path.dirname(process.execPath)+path.delimiter+process.env.PATH,NODE_ENV:name.endsWith('-web')?'development':(process.env.NODE_ENV||''),...extraEnv}});}
  finally{fs.closeSync(fd);}
  const launchedAt=Date.now();let ended=false;
  entry.pid=child.pid||null;save();log(name+' started PID '+child.pid);
  function restart(reason){
   if(ended)return;ended=true;entry.pid=null;save();log(name+' '+reason);
   if(closing)return;
   failures=Date.now()-launchedAt>60000?1:failures+1;
   const delay=Math.min(30000,5000*failures);entry.restarts++;save();
   log(name+' restarting in '+delay/1000+' seconds');setTimeout(launch,delay);
  }
  child.on('error',e=>restart('failed: '+e.message));child.on('exit',code=>restart('exited '+code));
 }
 launch();
}
function startApps(){
 if(started)return;started=true;
 const javaHome=process.env.MALL_JAVA_HOME||'D:/develop/jdk-17';
 for(const name of ['monitor','auth','gateway','admin','portal','search']){
  const agentJar=path.join(run,'agent-jars','mall-'+name+'.jar');
  const jar=agentConfig?.enabled&&fs.existsSync(agentJar)?agentJar:path.join(root,'jars','mall-'+name+'.jar');const config=path.join(root,'config',name+'.yaml').replace(/\\/g,'/');
  const encodingArgs=name==='gateway'?['-Dfile.encoding=UTF-8']:[];
  const javaEnv={MANAGEMENT_ENDPOINTS_WEB_EXPOSURE_INCLUDE:'health,info,prometheus',MANAGEMENT_ENDPOINT_ENV_SHOW_VALUES:'never',MANAGEMENT_ENDPOINT_CONFIGPROPS_SHOW_VALUES:'never'};
  if(name==='portal'&&agentConfig?.enabled)javaEnv.MALL_AGENT_BRIDGE_SECRET=agentConfig.bridgeSecret;
  if(name==='admin'&&agentConfig?.enabled&&runtimeConfig)javaEnv.MALL_AGENT_STAFF_BRIDGE_SECRET=runtimeConfig.staffBridgeSecret;
  startChild(name,path.join(javaHome,'bin/java.exe'),['-Xms128m','-Xmx512m','-XX:MaxMetaspaceSize=256m','-Duser.timezone=Asia/Shanghai',...encodingArgs,'-jar',jar,'--spring.profiles.active=deploy','--spring.config.additional-location=file:'+config],run,javaEnv);
 }
 const admin=path.join(workspace,'mall-admin-web'),shop=path.join(workspace,'mall-app-web');
 startChild('admin-web',process.execPath,[path.join(admin,'node_modules/vite/bin/vite.js'),'--host','127.0.0.1','--port','5100','--strictPort'],admin);
 startChild('shop-web',process.execPath,[path.join(shop,'node_modules/@dcloudio/vite-plugin-uni/bin/uni.js'),'-p','h5','--host','127.0.0.1','--port','5173','--strictPort'],shop);
 if(agentConfig?.enabled){
  const agent=path.join(workspace,'mall-agent'), python=path.join(agent,'.venv','Scripts','python.exe');
  const config={AGENT_DATABASE_URL:agentConfig.databaseUrl,AGENT_BROKER_SECRET:agentConfig.brokerSecret,
   AGENT_BRIDGE_SECRET:agentConfig.bridgeSecret,AGENT_CURSOR_SECRET:agentConfig.cursorSecret,
   LANGSMITH_TRACING:'false',PYTHONUTF8:'1'};
  const vectorPath=path.join(run,'milvus-env.json');
  const vector=fs.existsSync(vectorPath)?JSON.parse(fs.readFileSync(vectorPath)):null;
  const reader=vector?{AGENT_MILVUS_DATABASE:vector.database,AGENT_MILVUS_READER_USER:vector.readerUser,AGENT_MILVUS_READER_PASSWORD:vector.readerPassword}:{};
  const builder=vector?{...reader,AGENT_MILVUS_BUILDER_USER:vector.builderUser,AGENT_MILVUS_BUILDER_PASSWORD:vector.builderPassword}:{};
  startChild('agent-api',python,['-m','app.serve'],agent,{...config,...reader});
  startChild('agent-generation',python,['-m','app.workers.generation'],agent,{...config,...reader});
  startChild('agent-scheduler',python,['-m','app.workers.scheduler'],agent,config);
  startChild('agent-jobs',python,['-m','app.workers.jobs'],agent,{...config,...builder});
  startChild('agent-outbox',python,['-m','app.workers.outbox'],agent,config);
  startChild('agent-backups',python,[path.join(root,'backup-executor.py')],agent,{...config,AGENT_BACKUPS_ENABLED:'true'});
  startChild('agent-observability',python,[path.join(root,'monitor-agent.py')],agent,{...config,...reader,AGENT_OBSERVABILITY_ENABLED:'true'});
 }
}
function connect(){
 if(closing)return;const c=new Client();client=c;
 c.on('ready',()=>{connected=true;log('SSH connected; server middleware available through local loopback ports.');startApps();});
 c.on('error',e=>log('SSH: '+e.message));
 c.on('close',()=>{connected=false;if(!closing){log('SSH disconnected; retrying in 5 seconds.');setTimeout(connect,5000);}});
 c.connect({...sshConfig,readyTimeout:25000,keepaliveInterval:10000,keepaliveCountMax:5});
}
for(const [local,remote] of forwards){
 const server=net.createServer(socket=>{
  if(!connected)return socket.destroy();
  client.forwardOut('127.0.0.1',local,'127.0.0.1',remote,(err,stream)=>{
   if(err){socket.destroy();return;}socket.pipe(stream).pipe(socket);
   socket.on('error',()=>stream.destroy());stream.on('error',()=>socket.destroy());
   socket.on('close',()=>stream.destroy());stream.on('close',()=>socket.destroy());
  });
 });
 server.on('error',e=>{log('Tunnel port '+local+': '+e.message);process.exit(1);});
 server.listen(local,'127.0.0.1');servers.push(server);
}
const assetRoot=path.resolve(root,'web/demo-assets');
const mime={'.jpg':'image/jpeg','.jpeg':'image/jpeg','.png':'image/png','.gif':'image/gif','.webp':'image/webp','.svg':'image/svg+xml'};
const assets=http.createServer((req,res)=>{
 const u=new URL(req.url,'http://127.0.0.1:18080');
 if(u.pathname==='/health'){res.writeHead(200,{'Content-Type':'application/json'});return res.end(JSON.stringify({sshConnected:connected,maintenance:maintenanceState(path.join(run,'maintenance.json'))}));}
 if(!u.pathname.startsWith('/demo-assets/')){res.writeHead(404);return res.end();}
 const file=path.resolve(assetRoot,decodeURIComponent(u.pathname.slice('/demo-assets/'.length)));
 if(!file.startsWith(assetRoot+path.sep)){res.writeHead(403);return res.end();}
 fs.stat(file,(err,stat)=>{if(err||!stat.isFile()){res.writeHead(404);return res.end();}
  res.writeHead(200,{'Content-Type':mime[path.extname(file)]||'application/octet-stream','Content-Length':stat.size,'Cache-Control':'public,max-age=3600','Access-Control-Allow-Origin':'*'});fs.createReadStream(file).pipe(res);
 });
});
assets.on('error',e=>{log('Asset server: '+e.message);process.exit(1);});assets.listen(18080,'127.0.0.1');servers.push(assets);
for(const signal of ['SIGINT','SIGTERM'])process.on(signal,()=>{
 closing=true;client?.end();servers.forEach(x=>x.close());
 const stops=children.filter(x=>x.pid).map(child=>new Promise(resolve=>{
  if(process.platform==='win32'){
   const killer=spawn('taskkill.exe',['/PID',String(child.pid),'/T','/F'],{windowsHide:true,stdio:'ignore'});
   killer.once('exit',resolve);killer.once('error',resolve);
  }else{try{process.kill(child.pid);}catch{}resolve();}
 }));
 Promise.allSettled(stops).finally(()=>process.exit(0));
});
save();connect();
