const fs=require('node:fs'),path=require('node:path'),net=require('node:net'),http=require('node:http');
const {spawn}=require('node:child_process');
const {Client}=require('ssh2');
const root=__dirname,workspace=path.resolve(root,'..'),run=path.join(root,'.run');
fs.mkdirSync(run,{recursive:true});
const sshConfig=JSON.parse(fs.readFileSync(path.join(root,'.dev-ssh.json')));
const forwards=[[13306,3306],[16379,6379],[27018,27017],[15672,5672],[15673,15672],[18848,8848],[19848,9848],[19200,9200],[19000,9000],[19001,9001]];
const children=[],servers=[];let client,connected=false,started=false,closing=false;
const log=s=>console.log(new Date().toISOString()+' '+s);
function save(){fs.writeFileSync(path.join(run,'pids.json'),JSON.stringify({manager:process.pid,startedAt:new Date().toISOString(),children},null,2));}
function startChild(name,exe,args,cwd){
 const fd=fs.openSync(path.join(run,name+'.log'),'a');
 const child=spawn(exe,args,{cwd,windowsHide:true,stdio:['ignore',fd,fd],env:{...process.env,PATH:path.dirname(process.execPath)+path.delimiter+process.env.PATH,NODE_ENV:name.endsWith('-web')?'development':(process.env.NODE_ENV||'')}});
 children.push({name,pid:child.pid});save();log(name+' started PID '+child.pid);
 child.on('error',e=>log(name+' failed: '+e.message));child.on('exit',code=>log(name+' exited '+code));
}
function startApps(){
 if(started)return;started=true;
 const javaHome=process.env.MALL_JAVA_HOME||'D:/develop/jdk-17';
 for(const name of ['monitor','auth','gateway','admin','portal','search']){
  const jar=path.join(root,'jars','mall-'+name+'.jar');const config=path.join(root,'config',name+'.yaml').replace(/\\/g,'/');
  startChild(name,path.join(javaHome,'bin/java.exe'),['-Xms128m','-Xmx512m','-XX:MaxMetaspaceSize=256m','-Duser.timezone=Asia/Shanghai','-jar',jar,'--spring.profiles.active=deploy','--spring.config.additional-location=file:'+config],run);
 }
 const admin=path.join(workspace,'mall-admin-web'),shop=path.join(workspace,'mall-app-web');
 startChild('admin-web',process.execPath,[path.join(admin,'node_modules/vite/bin/vite.js'),'--host','127.0.0.1','--port','5100','--strictPort'],admin);
 startChild('shop-web',process.execPath,[path.join(shop,'node_modules/@dcloudio/vite-plugin-uni/bin/uni.js'),'-p','h5','--host','127.0.0.1','--port','5173','--strictPort'],shop);
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
 if(u.pathname==='/health'){res.writeHead(200,{'Content-Type':'application/json'});return res.end(JSON.stringify({sshConnected:connected}));}
 if(!u.pathname.startsWith('/demo-assets/')){res.writeHead(404);return res.end();}
 const file=path.resolve(assetRoot,decodeURIComponent(u.pathname.slice('/demo-assets/'.length)));
 if(!file.startsWith(assetRoot+path.sep)){res.writeHead(403);return res.end();}
 fs.stat(file,(err,stat)=>{if(err||!stat.isFile()){res.writeHead(404);return res.end();}
  res.writeHead(200,{'Content-Type':mime[path.extname(file)]||'application/octet-stream','Content-Length':stat.size,'Cache-Control':'public,max-age=3600','Access-Control-Allow-Origin':'*'});fs.createReadStream(file).pipe(res);
 });
});
assets.on('error',e=>{log('Asset server: '+e.message);process.exit(1);});assets.listen(18080,'127.0.0.1');servers.push(assets);
for(const signal of ['SIGINT','SIGTERM'])process.on(signal,()=>{closing=true;client?.end();servers.forEach(x=>x.close());for(const child of children){try{process.kill(child.pid);}catch{}}process.exit(0);});
save();connect();
