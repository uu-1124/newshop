// Add scoped runtime accounts without changing the existing mall accounts or vhost.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const {Client}=require('ssh2');
const root=__dirname,run=path.join(root,'.run');
const target=path.join(run,'runtime-env.json');
let cfg=fs.existsSync(target)?JSON.parse(fs.readFileSync(target)):null;
if(!cfg){cfg={rabbitUser:'mall_agent',rabbitPassword:crypto.randomBytes(30).toString('hex'),
  redisUser:'mall_agent',redisPassword:crypto.randomBytes(30).toString('hex'),
  staffBridgeSecret:crypto.randomBytes(32).toString('hex')};fs.writeFileSync(target,JSON.stringify(cfg,null,2));}
const secrets=JSON.parse(fs.readFileSync(path.join(root,'secrets.json')));
const script=`import json,subprocess,os
cfg=json.loads(${JSON.stringify(JSON.stringify(cfg))})
redis_password=${JSON.stringify(secrets.redis)}
containers=subprocess.run(['docker','ps','--format','{{.Names}}'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,universal_newlines=True,check=True).stdout.splitlines()
redis=next(n for n in containers if 'redis' in n)
rabbit=next(n for n in containers if 'rabbit' in n)
def command(args):
 p=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,universal_newlines=True)
 if p.returncode:raise RuntimeError('Provisioning command failed: '+args[0])
 return p.stdout.strip()
vhosts=command(['docker','exec',rabbit,'rabbitmqctl','list_vhosts','--silent'])
if '/mall-agent' not in vhosts.splitlines():command(['docker','exec',rabbit,'rabbitmqctl','add_vhost','/mall-agent'])
users=command(['docker','exec',rabbit,'rabbitmqctl','list_users','--silent'])
if not any(line.split()[0]==cfg['rabbitUser'] for line in users.splitlines() if line.split()):
 command(['docker','exec',rabbit,'rabbitmqctl','add_user',cfg['rabbitUser'],cfg['rabbitPassword']])
resource='^mall-agent(-[a-z0-9]+)?[.].*'
command(['docker','exec',rabbit,'rabbitmqctl','set_permissions','-p','/mall-agent',cfg['rabbitUser'],resource,resource,resource])
command(['docker','exec',rabbit,'rabbitmqctl','set_user_tags',cfg['rabbitUser']])
def rc(*args):return command(['docker','exec','-e','REDISCLI_AUTH='+redis_password,redis,'redis-cli','--raw',*args])
acl=['reset','on','>'+cfg['redisPassword'],'~mall-agent:*','~mall-agent-*:*','&mall-agent:*','&mall-agent-*:*','+ping','+publish','+subscribe','+unsubscribe','+psubscribe','+punsubscribe','+client|setinfo']
if rc('ACL','SETUSER',cfg['redisUser'],*acl)!='OK':raise RuntimeError('Redis ACL rejected')
info=json.loads(command(['docker','inspect',redis]))[0]
mounts=info['Mounts']
conf=next((m['Source'] for m in mounts if m['Destination'].endswith('redis.conf')),None)
if not conf:
 config_file=next((line.split(':',1)[1].strip() for line in rc('INFO','server').splitlines() if line.startswith('config_file:')),None)
 if config_file:
  for mount in mounts:
   destination=mount['Destination'].rstrip('/')
   if config_file.startswith(destination+'/'):
    conf=os.path.join(mount['Source'],config_file[len(destination)+1:]);break
acl_line='user '+cfg['redisUser']+' '+' '.join(acl)+'\\n'
if conf and os.path.isfile(conf):
 data=open(conf).read(); lines=[line for line in data.splitlines(True) if not line.startswith('user '+cfg['redisUser']+' ')]
 if any(line.strip().startswith('aclfile ') for line in lines):raise RuntimeError('Existing aclfile requires explicit support')
 with open(conf,'w') as f:f.write(''.join(lines).rstrip()+'\\n'+acl_line)
else:
 # The demo Redis starts from command arguments and only mounts /data. Persist its
 # complete ACL (including the unchanged default user) there, and append aclfile
 # to that service's existing compose command so a recreation retains the user.
 labels=info['Config'].get('Labels',{})
 compose_files=labels.get('com.docker.compose.project.config_files','').split(',')
 service=labels.get('com.docker.compose.service')
 if len(compose_files)!=1 or not os.path.isfile(compose_files[0]):raise RuntimeError('Redis compose source is ambiguous')
 source=compose_files[0]
 import yaml,shlex,shutil
 configuration=yaml.safe_load(open(source))
 spec=configuration['services'][service]
 arguments=spec.get('command',info['Config']['Cmd'])
 if isinstance(arguments,str):arguments=shlex.split(arguments)
 acl_file='/data/mall-agent-users.acl'
 if '--aclfile' in arguments:
  if arguments[arguments.index('--aclfile')+1]!=acl_file:raise RuntimeError('Existing Redis aclfile differs')
 else:arguments=arguments+['--aclfile',acl_file]
 data_mount=next((m['Source'] for m in mounts if m['Destination']=='/data'),None)
 if not data_mount:raise RuntimeError('Redis durable data mount missing')
 acl_host=os.path.join(data_mount,'mall-agent-users.acl')
 # Redis protects aclfile from CONFIG SET; export the complete live ACL instead.
 with open(acl_host,'w') as f:f.write(rc('ACL','LIST')+'\\n')
 uid,gid=command(['docker','exec',redis,'stat','-c','%u:%g','/proc/1']).split(':')
 os.chown(acl_host,int(uid),int(gid));os.chmod(acl_host,0o600)
 backup=source+'.before-agent-runtime'
 if not os.path.exists(backup):shutil.copy2(source,backup);os.chmod(backup,0o600)
 spec['command']=arguments
 temporary=source+'.agent-runtime.tmp'
 with open(temporary,'w') as f:yaml.safe_dump(configuration,f,allow_unicode=True,default_flow_style=False)
 os.chmod(temporary,os.stat(source).st_mode & 0o777);os.replace(temporary,source)
 command(['docker','compose','-f',source,'up','-d','--no-deps',service])
 import time
 for attempt in range(20):
  try:
   if rc('PING')=='PONG':break
  except RuntimeError:pass
  time.sleep(1)
 else:raise RuntimeError('Redis readiness timeout')
 if cfg['redisUser'] not in rc('ACL','USERS').splitlines():raise RuntimeError('Redis ACL did not survive recreation')
print(json.dumps({'result':'passed','rabbit_vhost':'/mall-agent','redis_acl':'mall_agent','redis_persistence':'durable ACL file','containers':[redis,rabbit]}))
`;
const c=new Client();
c.on('ready',()=>c.exec('python3 -',(err,stream)=>{
 if(err){c.end();throw new Error('Runtime provisioning SSH failed');}
 stream.on('data',d=>process.stdout.write(d));
 let diagnostic='';stream.stderr.on('data',d=>diagnostic+=d);
 stream.on('close',code=>{c.end();if(code){console.error('Scoped runtime provisioning failed; private diagnostic retained');fs.writeFileSync(path.join(run,'runtime-provision-error.log'),diagnostic);process.exitCode=1;}else{
  const envPath=path.join(root,'..','mall-agent','.env');let env=fs.readFileSync(envPath,'utf8');
  const values={AGENT_STAFF_BRIDGE_SECRET:cfg.staffBridgeSecret,AGENT_REDIS_URL:`redis://${cfg.redisUser}:${cfg.redisPassword}@127.0.0.1:16379/0`,
   AGENT_RABBIT_URL:`amqp://${cfg.rabbitUser}:${cfg.rabbitPassword}@127.0.0.1:15672/%2Fmall-agent`,AGENT_NOTIFICATIONS_ENABLED:'true',AGENT_BUILD:'runtime-0.5'};
  for(const [k,v] of Object.entries(values)){const re=new RegExp('^'+k+'=.*$','m');env=re.test(env)?env.replace(re,k+'='+v):env.trimEnd()+'\n'+k+'='+v+'\n';}
  fs.writeFileSync(envPath,env);
 }});stream.end(script);
}));
c.on('error',()=>{console.error('Runtime provisioning connection unavailable');process.exitCode=1;});
c.connect({...JSON.parse(fs.readFileSync(path.join(root,'.dev-ssh.json'))),readyTimeout:25000});
