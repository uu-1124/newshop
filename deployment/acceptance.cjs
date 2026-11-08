// Run against this disposable demonstration installation. --write-demo creates two demo orders.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const base='http://127.0.0.1:8201';
const evidence={time:new Date().toISOString(),architecture:'Local Java and Vue; server middleware over SSH',checks:[]};
fs.mkdirSync(path.join(__dirname,'evidence'),{recursive:true});
const save=()=>fs.writeFileSync(path.join(__dirname,'evidence',process.argv.includes('--write-demo')?'acceptance.json':'acceptance-read.json'),JSON.stringify(evidence,null,2));
async function api(route,{method='GET',token,json,form}={}){
 const headers={};if(token)headers.Authorization=token;if(json!==undefined)headers['Content-Type']='application/json';
 const res=await fetch(base+route,{method,headers,body:json!==undefined?JSON.stringify(json):form?new URLSearchParams(form):undefined,signal:AbortSignal.timeout(30000)});
 const result=await res.json();evidence.checks.push({method,route,status:res.status,code:result.code});save();
 assert.equal(res.status,200,route+' HTTP '+res.status);assert.equal(result.code,200,route+' '+result.message);return result.data;
}
async function check(name,fn){const result=await fn();console.log('PASS '+name);return result;}
async function main(){
 for(const [name,port] of Object.entries({admin:8080,portal:8085,search:8081,auth:8401,gateway:8201,monitor:8101}))await check('health '+name,async()=>{
  const r=await fetch('http://127.0.0.1:'+port+'/actuator/health');assert.equal(r.status,200);assert.equal((await r.json()).status,'UP');
 });
 for(const port of [5100,5173])await check('frontend '+port,async()=>{const r=await fetch('http://127.0.0.1:'+port+'/');assert.equal(r.status,200);assert.match(await r.text(),/<html/i);});
 const pwd=JSON.parse(fs.readFileSync(path.join(__dirname,'secrets.json'))).demoPassword;
 const a=await check('admin login',()=>api('/mall-admin/admin/login',{method:'POST',json:{username:'admin',password:pwd}}));const admin=a.tokenHead+a.token;
 const u=await check('member login',()=>api('/mall-portal/sso/login',{method:'POST',form:{username:'member',password:pwd}}));const member=u.tokenHead+u.token;
 await check('admin permissions',()=>api('/mall-admin/admin/info',{token:admin}));
 const products=await check('admin product list',()=>api('/mall-admin/product/list?pageNum=1&pageSize=100',{token:admin}));evidence.products=products.total;
 await check('admin order list',()=>api('/mall-admin/order/list?pageNum=1&pageSize=5',{token:admin}));
 const home=await check('home and advertisements',()=>api('/mall-portal/home/content'));
 await check('categories',()=>api('/mall-portal/product/categoryTreeList'));
 const list=await check('buyer product list',()=>api('/mall-portal/product/search?pageNum=1&pageSize=100'));evidence.publishedProducts=list.total;
 await check('product images',async()=>{
  const urls=[...new Set([...list.list.map(x=>x.pic),...home.advertiseList.map(x=>x.pic)])].filter(Boolean);
  for(const url of urls){const r=await fetch(url,{signal:AbortSignal.timeout(10000)});assert.equal(r.status,200,'image '+url);assert.match(r.headers.get('content-type'),/^image\//);}
  evidence.imagesChecked=urls.length;
 });
 if(process.argv.includes('--write-demo'))await check('search import',()=>api('/mall-search/esProduct/importAll',{method:'POST'}));
 const search=await check('Elasticsearch Chinese search',()=>api('/mall-search/esProduct/search?keyword='+encodeURIComponent('小米')+'&pageNum=1&pageSize=5'));assert(search.total>0);evidence.searchResults=search.total;
 await check('member profile',()=>api('/mall-portal/sso/info',{token:member}));
 if(!process.argv.includes('--write-demo')){evidence.status='read checks passed';save();return;}
 await check('MinIO upload, read and delete',async()=>{
  const form=new FormData();form.set('file',new Blob([Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aRz8AAAAASUVORK5CYII=','base64')],{type:'image/png'}),'deployment-check-'+Date.now()+'.png');
  const r=await fetch(base+'/mall-admin/minio/upload',{method:'POST',headers:{Authorization:admin},body:form,signal:AbortSignal.timeout(30000)});const b=await r.json();assert.equal(b.code,200,'MinIO upload');
  const file=await fetch(b.data.url);assert.equal(file.status,200);assert.match(file.headers.get('content-type'),/^image\//);
  evidence.uploadedImageAccessible=true;const objectName=new URL(b.data.url).pathname.split('/').slice(2).join('/');await api('/mall-admin/minio/delete?objectName='+encodeURIComponent(objectName),{method:'POST',token:admin});
 });
 let addresses=await api('/mall-portal/member/address/list',{token:member});let address=addresses.find(x=>x.name==='部署验收买家');
 if(!address){await api('/mall-portal/member/address/add',{method:'POST',token:member,json:{name:'部署验收买家',phoneNumber:'13800138000',defaultStatus:1,postCode:'100000',province:'北京市',city:'北京市',region:'东城区',detailAddress:'模拟收货地址，仅用于开发验收'}});addresses=await api('/mall-portal/member/address/list',{token:member});address=addresses.find(x=>x.name==='部署验收买家');}
 assert(address);
 const initialCart=await api('/mall-portal/cart/list',{token:member});
 const detail=await api('/mall-portal/product/detail/26');const product=detail.product;const sku=detail.skuStockList.find(x=>x.stock-x.lockStock>2&&!initialCart.some(c=>c.productSkuId===x.id));assert(sku);
 const baseline={stock:sku.stock,lockStock:sku.lockStock};
 async function createOrder(){
  const oldCart=await api('/mall-portal/cart/list',{token:member});assert(!oldCart.some(c=>c.productSkuId===sku.id),'Test SKU must not merge with existing cart');
  await api('/mall-portal/cart/add',{method:'POST',token:member,json:{productId:product.id,productSkuId:sku.id,quantity:1,price:sku.price,productPic:product.pic,productName:product.name,productSubTitle:product.subTitle,productSkuCode:sku.skuCode,productCategoryId:product.productCategoryId,productBrand:product.brandName,productSn:product.productSn,productAttr:sku.spData}});
  const cart=(await api('/mall-portal/cart/list',{token:member})).filter(c=>c.productSkuId===sku.id);assert.equal(cart.length,1);const cartIds=cart.map(x=>x.id);
  await api('/mall-portal/order/generateConfirmOrder',{method:'POST',token:member,json:cartIds});
  return (await api('/mall-portal/order/generateOrder',{method:'POST',token:member,json:{memberReceiveAddressId:address.id,cartIds,payType:1,useIntegration:0}})).order;
 }
 const order=await check('cart, confirm and create order',createOrder);evidence.paidOrderId=order.id;save();
 assert.equal((await api('/mall-portal/order/detail/'+order.id,{token:member})).status,0);
 await check('one simulated payment',()=>api('/mall-portal/order/paySuccess?orderId='+order.id+'&payType=1',{method:'POST',token:member}));
 assert.equal((await api('/mall-portal/order/detail/'+order.id,{token:member})).status,1);
 await check('admin delivery',()=>api('/mall-admin/order/update/delivery',{method:'POST',token:admin,json:[{orderId:order.id,deliveryCompany:'模拟物流',deliverySn:'DEMO'+order.id}]}));
 assert.equal((await api('/mall-portal/order/detail/'+order.id,{token:member})).status,2);
 await check('buyer confirms receipt',()=>api('/mall-portal/order/confirmReceiveOrder?orderId='+order.id,{method:'POST',token:member}));
 assert.equal((await api('/mall-portal/order/detail/'+order.id,{token:member})).status,3);
 const cancelled=await createOrder();evidence.cancelledOrderId=cancelled.id;save();
 await check('cancel unpaid order',()=>api('/mall-portal/order/cancelUserOrder?orderId='+cancelled.id,{method:'POST',token:member}));
 assert.equal((await api('/mall-portal/order/detail/'+cancelled.id,{token:member})).status,4);
 const end=(await api('/mall-portal/product/detail/26')).skuStockList.find(x=>x.id===sku.id);
 assert.equal(end.stock,baseline.stock-1);assert.equal(end.lockStock,baseline.lockStock);evidence.stock={baseline,final:{stock:end.stock,lockStock:end.lockStock}};
 evidence.status='All deployment acceptance checks passed';save();console.log(JSON.stringify({status:evidence.status,products:evidence.products,published:evidence.publishedProducts,paidOrderId:order.id,cancelledOrderId:cancelled.id,images:evidence.imagesChecked}));
}
main().catch(e=>{evidence.status='failed';evidence.error=e.message;save();console.error(e.message);process.exitCode=1;});
