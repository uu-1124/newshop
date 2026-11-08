const fs=require('node:fs/promises'),path=require('node:path'),crypto=require('node:crypto');
const root=__dirname;
const assetBase=process.env.MALL_ASSET_BASE||'http://127.0.0.1:18080';
async function main(){
 let sql=await fs.readFile(path.join(root,'../mall-swarm/document/sql/mall.sql'),'utf8');
 await fs.mkdir(path.join(root,'web/demo-assets'),{recursive:true});
 const urls=[...new Set(sql.match(/(?:https?:)?\/\/[^\s,'"<>\\]+/g)||[])].filter(u=>/\.(jpg|jpeg|png|gif|webp)(\?|$)/i.test(u));
 const results=[];let cursor=0;
 async function worker(){while(cursor<urls.length){const original=urls[cursor++];const normalized=original.startsWith('//')?'https:'+original:original.replace(/^http:/,'https:');const file=crypto.createHash('sha256').update(original).digest('hex').slice(0,20)+path.extname(new URL(normalized).pathname).toLowerCase();const dest=path.join(root,'web/demo-assets',file);let ok=false,error='';try{const st=await fs.stat(dest);ok=st.size>0;}catch{}
 if(!ok&&!original.includes('localhost:')){for(let i=0;i<2&&!ok;i++){try{const res=await fetch(normalized,{signal:AbortSignal.timeout(15000)});if(!res.ok)throw Error('HTTP '+res.status);const bytes=Buffer.from(await res.arrayBuffer());if(bytes.length<50)throw Error('Empty asset');await fs.writeFile(dest,bytes);ok=true;}catch(e){error=String(e);}}}
 sql=sql.split(original).join(assetBase+'/demo-assets/'+(ok?file:'unavailable.svg'));
 results.push({url:original,file,ok,error:ok?undefined:error});if(results.length%20===0)console.log('Processed '+results.length+'/'+urls.length);
 }}
 const r=await Promise.allSettled(Array.from({length:6},worker));for(const x of r)if(x.status==='rejected')throw x.reason;
 await fs.writeFile(path.join(root,'web/demo-assets/unavailable.svg'),'<svg xmlns="http://www.w3.org/2000/svg" width="600" height="600"><rect width="100%" height="100%" fill="#f3f4f6"/><text x="50%" y="50%" text-anchor="middle" dominant-baseline="middle" fill="#6b7280" font-family="sans-serif" font-size="32">演示图片暂缺</text></svg>');
 await fs.writeFile(path.join(root,'init/01-mall.sql'),sql);await fs.writeFile(path.join(root,'asset-manifest.json'),JSON.stringify(results,null,2));
 console.log(JSON.stringify({total:results.length,cached:results.filter(x=>x.ok).length,failed:results.filter(x=>!x.ok)}));
}
main().catch(e=>{console.error(e);process.exitCode=1;});
