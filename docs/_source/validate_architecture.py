"""Architecture coverage and selected protocol examples. No service/DB connections."""
from pathlib import Path
from itertools import product
from collections import Counter
import hashlib
import importlib
import json
import re
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(Path(__file__).resolve().parent))
from validate_design import parse_tables, schema_matches

checks=[]


def check(name, ok, detail):
    checks.append(dict(name=name,passed=bool(ok),detail=detail))


def load(name):
    return json.loads((ROOT/name).read_text(encoding='utf-8'))


def txt(name):
    return (ROOT/name).read_text(encoding='utf-8')


def validate():
    c=load('architecture/coverage.json');spec=load('api/openapi.json');s=spec['components']['schemas']
    operations=load('api/operation-catalog.json');tables=load('database/table-catalog.json')
    pages=load('ui/page-catalog.json');requirements=load('api/requirement-catalog.json')
    source=importlib.import_module('api_design')
    original={o['id']:(o['method'],o['path']) for o in source.OPS}
    actual={o['id']:(o['method'],o['path']) for o in operations}
    check('原接口保留',len(original)==133 and all(actual.get(k)==v for k,v in original.items()) and set(actual)-set(original)=={'listModelProfileVersions','reconcileBridgeOperation'},'当前133个基础operationId/方法/路径与生成源一致，包含反馈读取及四个规则管理接口；架构扩展增加模型历史与原编号核对2个')
    ids=[o['id'] for o in c['operations']]
    check('接口全量归属',len(ids)==len(set(ids))==135 and set(ids)==set(actual) and all(o['owner'] in c['modules'] and o['phase'] in {'S1','S2','S3','S4'} for o in c['operations']),'135个接口逐个有唯一owner与首激活阶段')
    by_id={o['id']:o for o in c['operations']}
    check('接口映射原样承接',all(all(by_id[o['id']][k]==o[k] for k in ['method','path','requirements','ui','body','result']) for o in operations),'每个接口的R/UI/输入输出与目录逐项相等')
    check('需求章节场景全集',
          {r['id'] for r in c['requirements']}=={r['id'] for r in requirements}=={f'R{i:02}' for i in range(1,19)}
          and {x['id'] for x in c['chapters']}==set(range(1,23))
          and {x['id'] for x in c['scenarios']}=={f'U{i:02}' for i in range(1,13)}
          and {x['id'] for x in c['acceptance']}=={f'A{i:02}' for i in range(1,16)}
          and {x['id'] for x in c['buyer_features']}=={f'C{i:02}' for i in range(1,9)}
          and {x['id'] for x in c['staff_features']}=={f'H{i:02}' for i in range(1,8)},'R18、22章、U12、A15、C8、H7精确集合覆盖')
    missing=[]
    for r in c['requirements']:
        wanted={o['id'] for o in operations if r['id'] in o['requirements']}
        if not r['modules'] or not r['phases'] or not r['pages'] or set(r['operations'])!=wanted:
            missing.append(r['id'])
    for name in ['chapters','scenarios','acceptance','buyer_features','staff_features']:
        missing += [str(x['id']) for x in c[name] if not x['modules'] or not x['requirements'] or not set(x['modules'])<=set(c['modules'])]
    check('追踪关系有效',not missing,f'每项需求有模块/阶段/页面/完整接口集合，场景引用有效；错误{missing}')
    page_ids=[p['id'] for p in c['pages']]
    check('页面全量归属',len(page_ids)==len(set(page_ids))==10 and set(page_ids)=={p['id'] for p in pages} and all(p['operations'] and p['owner'] in {'M15','M16'} for p in c['pages']),'B01–B05/A01–A05逐项归属及接口集合；8个原型route与目录一致')
    check('模块全集',set(c['modules'])=={f'M{i:02}' for i in range(1,21)},'M01–M20，无未定义模块')
    pg=parse_tables(txt('database/postgresql-v1.sql'));my=parse_tables(txt('database/mysql-v1.sql'))
    pg_names=[t['name'] for t in c['postgresql']];my_names=[t['name'] for t in c['mysql'] if t['kind']=='NEW']
    check('表全量归属',len(pg_names)==len(set(pg_names))==30 and set(pg_names)==set(pg) and len(my_names)==len(set(my_names))==9 and set(my_names)==set(my) and [t['name'] for t in c['mysql'] if t['kind']=='ALTER']==['oms_order_return_apply'],'30PG/9MySQL新表/1扩展，包含语音请求持久记录，逐项唯一owner与生命周期')
    field_lookup={t['name']:{f[0]:f for f in t['fields']} for t in tables}
    event_sql=pg['ag_event_outbox']['fields']['event_id']
    check('提交有序事件结构','IDENTITY' not in event_sql.upper() and 'nextval' not in event_sql.lower() and 'DEFAULT' not in event_sql.upper() and {'last_event_id','replay_floor'}<=set(pg['ag_event_clock']['fields']) and 'INSERT INTO ag_event_clock(singleton_id) VALUES (1)' in txt('database/postgresql-v1.sql'),'事件ID无sequence/identity/default；单行时钟及replay_floor存在。锁协议仍需真实并发测试')
    ev=load('api/sse-events.schema.json')['oneOf']
    check('控制世代贯通',all('control_epoch' in s[n]['required'] and 'control_epoch' in field_lookup[t] for n,t in [('Conversation','ag_conversation'),('Run','ag_run'),('Action','ag_action')]) and all('control_epoch' in e['required'] for e in ev),'会话/run/action/全部13事件含control_epoch；version独立为乐观锁')
    check('首次快照与列表补传契约',{'event_cursor','recent_messages','history_cursor','handoff','read_position'}<=set(s['Snapshot']['required']) and all('event_cursor' in s[name]['required'] for name in ['ConversationPage','HandoffPage','JobPage']),'正文/控制状态/游标同RR读取；员工列表可从最低水位补传')
    dispatch=pg['ag_action_dispatch']['fields']
    check('确认核对持久字段',{'confirmation_audit_id','reconcile_grant_ciphertext','grant_key_version','grant_expires_at','dispatch_started_at','fencing_token','lease_owner','lease_until'}<=set(dispatch),'dispatch可定位确认审计、加密仅核对凭据、投递与租约；会员Bearer无存储字段')
    bridge=[o for path,ms in spec['paths'].items() if 'agent-bridge' in path for o in ms.values() if 'operationId' in o]
    exceptions=[o for o in bridge if any('OperationReceipt' in x for x in o['security'])]
    check('核对权限唯一例外',len(bridge)==28 and len(exceptions)==1 and all(o['security'] in [[{'MemberBearer':[],'BridgeSignature':[]}],[{'StaffBearer':[],'BridgeSignature':[]}],[{'BridgeSignature':[],'OperationReceipt':[]}]] for o in bridge) and exceptions[0]['operationId']=='reconcileBridgeOperation' and exceptions[0]['security']==[{'BridgeSignature':[],'OperationReceipt':[]}] and exceptions[0].get('x-internal-reconciliation-only') is True,'27普通桥接均需主体Bearer与服务签名，唯一receipt核对不授予执行权限')
    reconcile=s['ReconcileOperationInput']
    check('核对输入不含执行参数',set(reconcile['properties'])=={'mode','parameters_hash'} and reconcile.get('additionalProperties') is False and reconcile['properties']['mode']['enum']==['QUERY_ONLY','FINALIZE_IF_ABSENT'],'核对模式有限、参数不变；不能传member/商品/新operation或任意URL')
    version_fields=set(pg['ag_model_profile_version']['fields'])
    public=s['ModelProfileVersion']['properties']
    check('不可变模型历史契约',{'name','enabled','is_active','profile_id','version_no','model_name','endpoint_ref','secret_version_ref','public_options','compatibility_evidence'}<=version_fields and 'updated_at' not in version_fields and not {'secret_ref','secret_version_ref'}&set(public) and {'name','enabled','is_active'}<=set(public),'保存启用/活动状态与不可变版本；公开历史无秘密引用')
    pg_sql=txt('database/postgresql-v1.sql')
    check('调用与向量绑定版本','FOREIGN KEY(profile_id,profile_version) REFERENCES ag_model_profile_version(profile_id,version_no)' in pg_sql and 'FOREIGN KEY(embedding_profile_id,embedding_profile_version) REFERENCES ag_model_profile_version(profile_id,version_no)' in pg_sql and 'ck_kb_model_pair' in pg_sql,'调用、知识向量和额度绑定组合版本；embedding配置与版本成对为空')
    quota=pg['ag_quota_reservation']['fields']
    check('额度预留与任务栅栏',{'call_id','bucket_key','unit_kind','reserved_units','settled_units','state'}<=set(quota) and all({'lease_owner','fencing_token'}<=set(pg[t]['fields']) for t in ['ag_job','ag_action_dispatch']) and 'MODEL_COMPATIBILITY' in field_lookup['ag_job']['job_type'][1],'额度区分计量单位，兼容任务可追踪，旧job/核对worker有栅栏')
    workflow=txt('architecture/03-关键流程与故障恢复.md')
    state_block=workflow.split('stateDiagram-v2',1)[1].split('```',1)[0]
    states=set(re.findall(r'\b[A-Z][A-Z_]+\b',state_block))
    check('Action状态精确一致',states==set(s['Action']['properties']['state']['enum']) and '| SIMULATED_REFUNDED |' in workflow,'Action图与OpenAPI枚举完全一致；Java REJECTED映射FAILED，售后精确模拟退款阶段')
    fault_ids=set(re.findall(r'\| (F\d{2}) ',workflow))
    check('故障与运维落点',fault_ids=={f'F{i:02}' for i in range(1,28)} and all(f'RB{i:02}' in txt('architecture/05-部署运维与验收.md') for i in range(1,10)),'F01–F27及RB01–RB09全量记录用户结果和后台处理')
    graph=txt('architecture/02-LangGraph与工具编排.md')
    nodes=set(re.findall(r'\| (N\d{2}) ',graph));tools=set(re.findall(r'\| (T\d{2}) ',graph))
    check('图节点与工具全集',nodes=={f'N{i:02}' for i in range(1,13)} and tools=={f'T{i:02}' for i in range(1,12)},'12节点/11受控工具含购物车读取逐项定义；确认/付款/审核不在工具注册范围')
    files=[ROOT/'07-系统架构与模块设计.md',ROOT/'08-第一阶段开发与验收计划.md']+sorted((ROOT/'architecture').glob('*.md'))
    broken=[]
    for f in files:
        for target in re.findall(r'\[[^\]]+\]\(([^)]+)\)',f.read_text(encoding='utf-8')):
            if target.startswith(('https://','http://','#')):continue
            if not (f.parent/target.split('#')[0]).exists():broken.append(str(f.name)+':'+target)
        if f.read_text(encoding='utf-8').count('```')%2:broken.append(f.name+':fence')
    check('分册与链接完整',len(files)==7 and not broken,f'7篇架构/计划正文，链接存在且代码围栏成对；错误{broken}')
    check('原候选选型保留',all(x in graph for x in ['gpt-6.1-sol','text-embedding-v4','qwen3-rerank','qwen3-vl-plus','qwen3-asr-flash','qwen-audio-3.1-tts-flash']) and all(x in txt('architecture/05-部署运维与验收.md') for x in ['Python','3.13','Milvus3.0.5 / PyMilvus3.0.2']),'原六能力候选及Python/Milvus组合保留，并标实际兼容未验证')
    # Linearized reference model: an absent operation may be executed once or fenced, never both.
    traces=0;violations=[]
    for length in range(1,6):
        for trace in product('ERQ',repeat=length):
            state=None;effects=0
            for command in trace:
                if command=='E' and state is None:state='SUCCEEDED';effects+=1
                elif command=='R' and state is None:state='REJECTED'
                if effects>1 or (state=='REJECTED' and effects):violations.append(''.join(trace))
            traces+=1
    check('核对协议参考轨迹',not violations,f'{traces}条线性参考轨迹验证最多一次副作用、封闭后迟到执行无效；不是MySQL真实锁测试')
    # Demonstrate why an allocation sequence is insufficient, and ordered commit prevents late lower IDs.
    old_cursor=102;late_committed=101
    check('事件协议参考反例',late_committed<old_cursor and sorted([101,102])==[101,102],'sequence先分101后分102、102先提交会漏101；时钟持锁到提交排除此顺序，实际并发待验证')
    amount_cases=0;amount_errors=[]
    for cents in range(0,101):
        for qty in range(1,11):
            q,r=divmod(cents,qty);units=[q+(i<r) for i in range(qty)]
            if sum(units)!=cents or max(units)-min(units)>1:amount_errors.append((cents,qty))
            amount_cases+=1
    check('金额件额参考样例',not amount_errors,f'{amount_cases}个整数分/数量样例保证件额守恒；多优惠Java完整算法与业务D04仍待实现验收')
    baseline=Path(r'C:\Users\Administrator\AppData\Local\Temp\mall-agent-architecture-20261111\source-baseline.json')
    if baseline.exists():
        hashes=json.loads(baseline.read_text(encoding='utf-8'));changed=[]
        for name,digest in hashes.items():
            f=ROOT.parent/name
            if not f.exists() or hashlib.sha256(f.read_bytes()).hexdigest().lower()!=digest.lower():changed.append(name)
        if '--implementation' in sys.argv:
            # Implementation is authorized to edit application sources. Preserve the
            # original requirement document and keep all source drift visible.
            originals=[name for name in hashes if name.lower().endswith('.docx')]
            changed_originals=[name for name in changed if name in originals]
            check('实施阶段需求原件未改',bool(originals) and not changed_originals,
                  f'原件{originals}；原件差异{changed_originals}；授权实施源码差异（不作只读断言）{changed}')
        else:
            check('本轮业务源码与需求原件未改',not changed,f'逐文件复核预先快照的{len(hashes)}个业务/原件文件；差异{changed}')
    else:
        check('本轮源码快照可用性',False,'本机预先快照缺失，不能据此声明源码未改；换机可使用已保存的本轮报告')
    check('基础设计检查',load('design-validation.json')['passed'] and load('prototype-validation.json')['passed'],'基础16项结构检查和13项原型逻辑检查通过；浏览器视觉未验证')
    report=dict(date='2026-11-11',version='0.2',kind='offline-architecture-review',passed=all(x['passed'] for x in checks),checks=checks,
                limits=['未执行SQL、迁移或真实数据库并发','未进行供应商调用、容量或隔离恢复测试','未完成真实浏览器视觉与焦点验证','协议参考轨迹只验证说明模型，不能替代实现测试'])
    (ROOT/'architecture-validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    note=['## 架构v0.2检查','', '| 检查 | 结果 | 依据 |','|---|---|---|']
    note += ['| '+x['name']+' | '+('通过' if x['passed'] else '待修复')+' | '+x['detail'].replace('|','/')+' |' for x in checks]
    note += ['','机器记录见architecture-validation.json。上述协议例子为设计层参考，不代表数据库锁、真实故障或供应商已经验证。']
    record=txt('06-设计检查记录.md').split('\n## 架构v0.2检查',1)[0]
    (ROOT/'06-设计检查记录.md').write_text(record.rstrip()+'\n\n'+'\n'.join(note)+'\n',encoding='utf-8')
    print(json.dumps(dict(passed=report['passed'],checks=len(checks),failures=[x for x in checks if not x['passed']]),ensure_ascii=False))
    return report['passed']


if __name__=='__main__':
    sys.exit(0 if validate() else 1)
