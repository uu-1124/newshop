// Only the bundled read-only observer can be executed; no caller input or destination.
const fs=require('node:fs'),path=require('node:path');
const {Client}=require('./node_modules/ssh2');
const client=new Client();let total=0;
const timer=setTimeout(()=>{client.end();process.exitCode=1;},25000);
client.on('ready',()=>client.exec('python3 -',(error,stream)=>{
  if(error){client.end();process.exitCode=1;return;}
  stream.on('data',chunk=>{total+=chunk.length;if(total>16384){stream.destroy();client.end();process.exitCode=1;}else process.stdout.write(chunk);});
  stream.stderr.on('data',()=>{});
  stream.on('close',code=>{clearTimeout(timer);client.end();process.exitCode=code||0;});
  stream.end(fs.readFileSync(path.join(__dirname,'observe-remote.py'),'utf8'));
}));
client.on('error',()=>{clearTimeout(timer);process.stderr.write('Read-only remote observer unavailable\n');process.exitCode=1;client.end();});
client.connect({...JSON.parse(fs.readFileSync(path.join(__dirname,'.dev-ssh.json'),'utf8')),readyTimeout:15000});
