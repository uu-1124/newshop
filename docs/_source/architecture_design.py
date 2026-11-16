"""Generate exhaustive architecture ownership from the reviewed v0.2 contracts."""
from pathlib import Path
from collections import Counter
import json
import re
from docx import Document

ROOT = Path(__file__).resolve().parents[1]
MODULES = {
    "M01": "身份与授权", "M02": "会话与历史", "M03": "消息与事件", "M04": "运行与图编排",
    "M05": "受控业务工具", "M06": "业务确认与核对", "M07": "人工接待", "M08": "知识管理",
    "M09": "检索与引用", "M10": "媒体与附件", "M11": "模型提示词与额度", "M12": "留存与擦除",
    "M13": "审计统计与反馈", "M14": "任务与调度", "M15": "买家前端", "M16": "员工前端",
    "M17": "Java身份与查询", "M18": "Java计价与交易", "M19": "Java售后与物流", "M20": "部署与恢复",
}
# Every operation appears once here. New/renamed contract operations fail generation until reviewed.
OP_GROUPS = [
    ("M01", "S1", "memberMe adminMe"),
    ("M02", "S1", "createConversation listConversations getConversation renameConversation listMessages updateReadCursor"),
    ("M12", "S1", "deleteConversation"),
    ("M03", "S1", "sendMemberMessage memberEvents adminEvents"),
    ("M04", "S1", "getRun stopRun retryRun"),
    ("M06", "S2", "prepareAction getAction confirmAction cancelAction listUnresolvedActions resumeActionReconciliation"),
    ("M07", "S1", "requestHandoff withdrawHandoff setWaitingAI adminListConversations adminConversationSnapshot adminListMessages sendStaffMessage getStaffBusinessContext listStaffNotes createStaffNote adminReadCursor listHandoffs claimHandoff transferHandoff closeHandoff listStaff setPresence presenceHeartbeat updateStaffProfile"),
    ("M10", "S3", "uploadMemberAttachment readMemberAttachment transcribeAudio synthesizeSpeech"),
    ("M10", "S1", "uploadStaffAttachment readStaffAttachment"),
    ("M13", "S3", "submitFeedback readFeedback"),
    ("M05", "S1", "searchProducts getProduct"),
    ("M05", "S2", "getCheckoutOptions listMemberOrders getMemberOrder getMemberLogistics listOrderReturns checkReturnEligibility"),
    ("M09", "S1", "readCitation"),
    ("M08", "S1", "listKBDocuments createKBDocument getKBDocument updateKBDocument listKBVersions createKBVersion getKBVersion parseKBVersion previewKBVersion editKBPreview buildKBVersion publishKBVersion setKBEnabled"),
    ("M14", "S1", "listJobs getJob retryJob"),
    ("M11", "S1", "listModelProfiles getModelRegistry testModelProfile createModelProfile updateModelProfile activateModelProfile listModelProfileVersions listPrompts createPrompt publishPrompt getSettings updateSettings listUnknownModelUsage reconcileModelUsage"),
    ("M12", "S3", "previewRetention executeRetention"),
    ("M13", "S1", "getStatistics listAuditLogs"),
    ("M20", "S4", "verifyBackup listBackups createBackup updateBackupPolicy"),
    ("M17", "S1", "bridgeMemberIdentity bridgeProducts bridgeProductDetail bridgeStaffIdentity bridgeStaffContext"),
    ("M18", "S2", "bridgeCart bridgeCheckoutOptions bridgeQuote executeBridgeOperation getBridgeOperation reconcileBridgeOperation"),
    ("M17", "S2", "bridgeOrders bridgeOrder bridgeLogistics bridgeReturns bridgeReturnEligibility"),
    ("M19", "S2", "updateLogistics createLogisticsNode updateLogisticsNode reviewReturn receiveReturn simulateRefund bridgeStaffReturns bridgeReturnAddresses getStaffReturn getStaffLogistics bridgeListReturnRules bridgePublishReturnRule adminListReturnRules adminPublishReturnRule"),
    ("M05", "S2", "adminUpdateLogistics adminReadLogistics adminCreateLogisticsNode adminEditLogisticsNode adminListReturns adminReturnAddresses adminReadReturn adminReviewReturn adminReceiveReturn adminSimulateRefund"),
]
PG_OWNERS = {
    "M07": "ag_staff_profile ag_staff_note ag_handoff",
    "M02": "ag_conversation ag_read_cursor",
    "M03": "ag_message ag_event_outbox ag_event_clock",
    "M10": "ag_attachment ag_attachment_ref ag_media_request",
    "M04": "ag_run ag_generation_slot ag_context_summary",
    "M06": "ag_action ag_action_dispatch",
    "M11": "ag_model_profile ag_model_profile_version ag_prompt_version ag_quota_reservation ag_setting",
    "M08": "ag_kb_document ag_kb_version ag_kb_chunk",
    "M14": "ag_job",
    "M13": "ag_model_call ag_audit_log ag_feedback",
    "M01": "ag_request_dedupe",
    "M12": "ag_erasure_tombstone",
}
MY_OWNERS = {
    "oms_agent_quote": ("M18", "S2", "报价有效期内可用；操作引用保护历史，不随聊天删除"),
    "oms_agent_operation": ("M18", "S2", "业务幂等最终账本；与业务数据同寿命，恢复优先核对"),
    "oms_after_sale_rule_version": ("M19", "S2", "不可变规则；被申请引用的版本禁止覆盖/删除"),
    "oms_return_proof_reference": ("M19", "S2", "售后保护引用；未知操作与有效申请期间保护字节"),
    "oms_shipment": ("M19", "S2", "模拟物流主对象；编辑不改变订单状态"),
    "oms_shipment_event": ("M19", "S2", "节点编辑留审计；与shipment同受控生命周期"),
    "oms_agent_audit": ("M18", "S2", "交易与员工业务责任记录，独立于普通聊天期限"),
    "oms_agent_request_dedupe": ("M19", "S2", "员工操作同事务结果；清理前保留业务唯一性与审计"),
    "oms_agent_business_outbox": ("M18", "S2", "未发布不得清理；发布归档与业务消费幂等独立"),
}
REQ_MODULES = {
    "R01": "M01 M02 M07 M10 M15 M16 M17", "R02": "M02 M03 M04 M12 M15",
    "R03": "M03 M04 M11 M14 M15", "R04": "M05 M15 M17", "R05": "M08 M09 M15 M16",
    "R06": "M01 M02 M03 M04 M06 M07 M15 M16 M17", "R07": "M08 M09 M10 M11 M14 M16",
    "R08": "M05 M06 M15 M18", "R09": "M05 M06 M15 M18", "R10": "M05 M15 M16 M17 M19",
    "R11": "M05 M06 M15 M18", "R12": "M05 M06 M10 M15 M16 M18 M19",
    "R13": "M03 M04 M10 M15 M16", "R14": "M10 M11 M15", "R15": "M13 M15",
    "R16": "M07 M11 M13 M16", "R17": "M02 M04 M10 M12 M14 M16 M20",
    "R18": "M01 M03 M04 M06 M11 M12 M13 M14 M18 M19 M20",
}
REQ_PHASES = {"R01":"S0 S1 S2 S3 S4", "R02":"S1 S3", "R03":"S1 S4", "R04":"S1",
              "R05":"S1", "R06":"S1 S2 S3", "R07":"S1", "R08":"S2", "R09":"S2",
              "R10":"S2", "R11":"S2", "R12":"S2 S3", "R13":"S3", "R14":"S3",
              "R15":"S3", "R16":"S1 S3 S4", "R17":"S0 S1 S3 S4", "R18":"S0 S1 S2 S3 S4"}
CHAPTER_MODULES = [
    "M01 M15 M16 M17", "M01 M04 M05 M06 M07 M08 M14 M17 M18 M19 M20",
    "M04 M11 M14 M20", "M10 M11 M13", "M02 M03 M04 M12 M15", "M05 M06 M15 M17 M18",
    "M05 M06 M10 M16 M17 M18 M19", "M01 M03 M04 M07 M16 M17", "M08 M09 M10 M11 M14 M16",
    "M04 M10 M11 M12 M15 M16", "M04 M05 M09 M11", "M01 M06 M18 M19",
    "M02 M03 M06 M08 M10 M11 M13 M18 M19", "M10 M12 M18 M19 M20",
    "M01 M02 M03 M04 M05 M06 M15", "M01 M07 M08 M11 M12 M13 M14 M16 M17 M18 M19",
    "M01 M03 M04 M06 M10 M11 M12 M13 M20", "M14 M20", "M15 M16 M18 M19 M20",
    "M17 M18 M19 M20", "M15 M16 M20", "M20",
]
CH_FEATURES = [
    ["R01"], ["R01","R18"], ["R18"], ["R14","R16"], ["R02","R03","R15"],
    ["R04","R08","R09","R11"], ["R10","R12"], ["R06"], ["R05","R07"],
    ["R13","R14","R17"], ["R03","R18"], ["R08","R09","R11","R12","R18"],
    ["R01","R18"], ["R12","R17","R18"], ["R01","R02","R03"],
    ["R01","R06","R07","R16","R17"], ["R01","R18"], ["R18"],
    [f"R{i:02}" for i in range(1,19)], ["R18"], [f"R{i:02}" for i in range(1,19)], [f"R{i:02}" for i in range(1,19)],
]
ACCEPT_REQ = {
    "A01":"R01", "A02":"R04 R05", "A03":"R08 R09 R18", "A04":"R11 R18", "A05":"R12 R18",
    "A06":"R06 R18", "A07":"R13 R14 R17", "A08":"R03 R06 R18", "A09":"R02 R15",
    "A10":"R05 R07", "A11":"R10", "A12":"R01 R16 R18", "A13":"R17", "A14":"R18", "A15":"R03 R18",
}
CLIENT_REQ = {"C01":"R01 R04 R10", "C02":"R02", "C03":"R03", "C04":"R04 R05 R08 R09 R10 R11 R12",
              "C05":"R03 R15 R18", "C06":"R06", "C07":"R13 R14", "C08":"R02 R03 R18"}
STAFF_REQ = {"H01":"R06", "H02":"R06 R18", "H03":"R06 R13", "H04":"R01 R06",
             "H05":"R06", "H06":"R06 R18", "H07":"R06"}


def load(file):
    return json.loads((ROOT/file).read_text(encoding="utf-8"))


def write(file, text):
    (ROOT/file).write_text(text.rstrip()+"\n",encoding="utf-8")


def md_table(headers, rows):
    def cell(value):
        if isinstance(value,list): value="、".join(map(str,value))
        return str(value).replace("|","&#124;").replace("\n","<br>")
    return ["| "+" | ".join(headers)+" |", "|"+"---|"*len(headers)] + ["| "+" | ".join(cell(v) for v in row)+" |" for row in rows]


def lifecycle(name):
    if name in {"ag_message","ag_conversation","ag_context_summary"}: return "普通文字默认90天；删除先不可见，清派生上下文，业务引用另行保护"
    if name == "ag_event_outbox": return "默认7天连续前缀；授权先过滤，清理原子推进replay_floor"
    if name == "ag_event_clock": return "单行长期保留；与事件同备份，不重置游标"
    if name in {"ag_action","ag_action_dispatch"}: return "确认与结果责任独立保留；UNKNOWN禁止清除核对与保护信息"
    if name in {"ag_attachment","ag_attachment_ref"}: return "普通图片90天/录音与播报7天；知识和售后保护覆盖普通到期"
    if name.startswith("ag_kb_"): return "活动/被引用版本保护；禁用立即阻止新引用，旧索引异步回收"
    if name in {"ag_model_profile_version","ag_prompt_version"}: return "不可变版本；仍被调用/知识引用时不得删除"
    if name in {"ag_model_call","ag_quota_reservation"}: return "分单位结算；未知计费保守占用；保留调用版本与对账证据"
    if name == "ag_erasure_tombstone": return "删除事实独立备份；恢复先重放，保留期覆盖可能恢复的备份"
    if name == "ag_audit_log": return "责任与结果独立策略；脱敏、受限访问、不随聊天级联删除"
    if name == "ag_request_dedupe": return "普通请求至少24小时建议；业务唯一键承担更长期幂等"
    if name in {"ag_run","ag_job","ag_generation_slot"}: return "租约/fencing；终态归档与源引用协调，失败重试须原权限"
    return "按源对象生命周期/业务策略保留，执行清理前检查外键、权限及审计引用"


def boundary(op, owner):
    if op['id']=="reconcileBridgeOperation": return "MySQL原操作唯一键：只查询或封闭缺失；PG另事务投影"
    if owner in {"M18","M19"}:
        return "MySQL业务+operation/dedupe+审计/outbox同事务" if op['method']!="GET" else "MySQL授权只读；不据缺失推定失败"
    if owner=="M17": return "Java认证/归属复核后的授权查询；POST也不等于业务写"
    if op['method']=="GET": return "PG/授权桥接读取；快照使用同一RR事务，SSE短查询"
    if op['id']=="confirmAction": return "PG确认审计+action+dispatch提交，再首次Java执行一次"
    if op['id']=="prepareAction": return "Java准备查询在锁外；PG保存不可变action及事件"
    if owner=="M05": return "桥接IO在PG锁外；Java拥有业务事务与员工幂等"
    if owner=="M10": return "存储/供应商IO在锁外；PG附件/引用/额度分阶段提交"
    if owner in {"M08","M12","M14","M20"}: return "PG状态/审计/job同事务；长任务异步、提交后唤醒"
    return "PG去重+领域状态+审计/事件同事务，事件时钟最后锁"


def generate():
    ops, tables, pages, reqs = [load(f) for f in ["api/operation-catalog.json","database/table-catalog.json","ui/page-catalog.json","api/requirement-catalog.json"]]
    ownership={}
    for owner, phase, names in OP_GROUPS:
        for name in names.split():
            assert name not in ownership, name
            ownership[name]=(owner,phase)
    assert set(ownership)=={o['id'] for o in ops}, (set(ownership)-{o['id'] for o in ops}, {o['id'] for o in ops}-set(ownership))
    pg_owners={}
    for owner,names in PG_OWNERS.items():
        for name in names.split():
            assert name not in pg_owners,name
            pg_owners[name]=owner
    assert set(pg_owners)=={t['name'] for t in tables}
    spec=load("api/openapi.json")
    operation_rows=[]
    for o in ops:
        owner,phase=ownership[o['id']]
        native=spec['paths'][o['path']][o['method'].lower()]
        item={**o,"owner":owner,"phase":phase,"security":native['security'],"boundary":boundary(o,owner)}
        item['frontend_owner']="M16" if any(p.startswith('A') for p in o['ui']) else "M15"
        item['activation_note']=("S1只允许知识原件用途；聊天图片S3" if o['id'] in {'uploadStaffAttachment','readStaffAttachment'} else
                                 "S1仅激活文字/知识能力与基础配置；媒体S3、清理执行S3" if o['id'] in {'getSettings','updateSettings','listModelProfiles','createModelProfile','updateModelProfile','activateModelProfile'} else
                                 "S1知识任务；S2核对；S3清理；S4备份。retry禁止恢复业务execute" if owner=='M14' else
                                 "按phase开放；跨阶段需求以对应验收为准")
        operation_rows.append(item)
    pg_rows=[{"name":t['name'],"title":t['title'],"owner":pg_owners[t['name']],"requirements":t['requirements'],
              "phase":"S2" if t['name'] in {'ag_action','ag_action_dispatch'} else "S3" if t['name']=='ag_feedback' else "S0/S1",
              "fields":len(t['fields']),"lifecycle":lifecycle(t['name'])} for t in tables]
    mysql_sql=(ROOT/'database/mysql-v1.sql').read_text(encoding='utf-8')
    mysql_names=re.findall(r'CREATE TABLE (\w+)',mysql_sql)
    assert set(mysql_names)==set(MY_OWNERS) and len(mysql_names)==len(set(mysql_names))
    my_rows=[dict(name=n,owner=MY_OWNERS[n][0],phase=MY_OWNERS[n][1],lifecycle=MY_OWNERS[n][2],kind='NEW') for n in mysql_names]
    my_rows.append(dict(name='oms_order_return_apply',owner='M19',phase='S2',lifecycle='新增明细/金额/规则/阶段/版本；历史歧义保持NULL，兼容旧status',kind='ALTER'))
    source=Document(ROOT/'商城智能体需求分析与技术设计.docx')
    scenarios=[]
    for p in source.paragraphs:
        m=re.match(r'(U\d{2})\s+(.+)',p.text)
        if m: scenarios.append({'id':m[1],'title':m[2],'requirements':[],'acceptance':[]})
        elif p.text.startswith('完成结果') and scenarios:
            scenarios[-1]['requirements']=re.findall(r'R\d{2}',p.text)
            scenarios[-1]['acceptance']=re.findall(r'A\d{2}',p.text)
    def modules(rids): return sorted({m for r in rids for m in REQ_MODULES[r].split()})
    def phases(rids): return sorted({s for r in rids for s in REQ_PHASES[r].split()})
    for u in scenarios: u.update(modules=modules(u['requirements']),phases=phases(u['requirements']))
    def features(index,mapping):
        return [dict(id=r.cells[0].text,title=r.cells[1].text,criteria=r.cells[2].text,
                     requirements=mapping[r.cells[0].text].split(),modules=modules(mapping[r.cells[0].text].split()),
                     phases=phases(mapping[r.cells[0].text].split())) for r in source.tables[index].rows[1:]]
    acceptance=features(15,ACCEPT_REQ)
    client=features(5,CLIENT_REQ)
    staff=features(6,STAFF_REQ)
    chapters=[]
    for p in source.paragraphs:
        m=re.match(r'(\d+)\s+(.+)',p.text)
        if p.style.name=='Heading 1' and m:
            n=int(m[1]); chapters.append(dict(id=n,title=m[2],modules=CHAPTER_MODULES[n-1].split(),requirements=CH_FEATURES[n-1]))
    requirement_rows=[]
    for r in reqs:
        rid=r['id']
        requirement_rows.append({**r,'modules':REQ_MODULES[rid].split(),'phases':REQ_PHASES[rid].split(),
            'operations':[o['id'] for o in operation_rows if rid in o['requirements']],
            'pages':[p['id'] for p in pages if rid in p['requirements'].split()],
            'pg_tables':[t['name'] for t in tables if rid in t['requirements']],
            'scenarios':[u['id'] for u in scenarios if rid in u['requirements']],
            'acceptance':[a['id'] for a in acceptance if rid in a['requirements']]})
    page_rows=[]
    for p in pages:
        rids=p['requirements'].split()
        page_rows.append({**p,'owner':'M15' if p['id'].startswith('B') else 'M16','modules':modules(rids),
                          'phases':phases(rids),'operations':[o['id'] for o in ops if p['id'] in o['ui']]})
    coverage=dict(version='0.2',date='2026-11-11',baseline='1.1',modules=MODULES,requirements=requirement_rows,
                  chapters=chapters,scenarios=scenarios,acceptance=acceptance,buyer_features=client,staff_features=staff,
                  operations=operation_rows,postgresql=pg_rows,mysql=my_rows,pages=page_rows)
    write('architecture/coverage.json',json.dumps(coverage,ensure_ascii=False,indent=2))
    lines=['# 数据、接口、页面与需求承接矩阵','',
           '架构v0.2，2026-11-11，需求基线1.1。本分册由已评审的显式模块归属与现有契约生成。覆盖意味着设计有承接，不表示功能已经实现或验收通过。字段逐项字典见02数据库与SQL，输入输出逐项见03接口与OpenAPI，页面交互见01UI。','',
           f'精确范围：18项需求、22章、12个U场景、15个A验收、8个C和7个H功能；{len(ops)}个operation、{len(tables)}张PG表、9张MySQL新表和1张扩展、10个逻辑页面。','',
           '## 1 模块与阶段','']
    lines+=md_table(['模块','职责','接口数','PG表数'],[[m,title,sum(o['owner']==m for o in operation_rows),sum(t['owner']==m for t in pg_rows)] for m,title in MODULES.items()])
    lines+=['','S0是工程/权限/事务基础；S1是文字咨询、导购、知识和真实人工；S2是交易售后物流；S3是媒体反馈和完整留存治理；S4是整体容量、恢复和发布验收。接口phase是首个激活阶段，不是整项需求完成阶段。','', '## 2 原需求18项逐项承接','']
    lines+=md_table(['需求/优先级','范围','模块','阶段','页面','U/A'],[[r['id']+' '+r['priority'],r['scope'],r['modules'],r['phases'],r['pages'],r['scenarios']+r['acceptance']] for r in requirement_rows])
    lines+=['','每项需求的精确operation及PG表集合保存在coverage.json.requirements；下方接口表逐项保留原R/UI引用。MySQL业务对象由第7节及Java模块承接，不能从PG表是否包含R编号推定Java需求缺失。','', '## 3 原文22章的架构落点','']
    lines+=md_table(['章','标题','模块','需求'],[[x['id'],x['title'],x['modules'],x['requirements']] for x in chapters])
    lines+=['','## 4 典型用户场景与验收','']
    lines+=md_table(['U场景','目标','需求','A验收','责任模块','阶段'],[[u['id'],u['title'],u['requirements'],u['acceptance'],u['modules'],u['phases']] for u in scenarios])
    lines+=['','A验收保留原文通过标准，阶段子集不能替代最终全项通过。','']
    lines+=md_table(['A验收','场景','原文通过标准','模块','阶段'],[[a['id'],a['title'],a['criteria'],a['modules'],a['phases']] for a in acceptance])
    lines+=['','## 5 买家C与员工H功能','']
    lines+=md_table(['功能','名称','原文要求','需求','模块'],[[f['id'],f['title'],f['criteria'],f['requirements'],f['modules']] for f in client+staff])
    lines+=['','## 6 全量接口责任与事务','',
            '普通Java桥接同时要求当前主体Bearer和BridgeSignature；唯一后台核对例外使用OperationReceipt+BridgeSignature。所有写方法含必填幂等键；POST只读查询也按统一契约去重。浏览器只调用mall-agent门面。owner是首要服务责任，前端责任依据UI为M15/M16；签名桥接不因有UI映射而开放给浏览器。','']
    lines+=md_table(['operationId','方法与路径','模块/阶段','鉴权/权限','R/UI','事务与恢复'],[[o['id'],o['method']+' '+o['path'],o['owner']+'/'+o['phase'],
          '; '.join('+'.join(s) for s in o['security'])+(' / '+o['permission'] if o.get('permission') else ''),o['requirements']+o['ui'],o['boundary']] for o in operation_rows])
    lines+=['','### 6.1 跨阶段接口激活限制','']
    notes={}
    for o in operation_rows:
        if not o['activation_note'].startswith('按phase'):notes.setdefault(o['activation_note'],[]).append(o['id'])
    lines+=md_table(['接口','限制'],[[ids,note] for note,ids in notes.items()])
    lines+=['','## 7 全量表责任与生命周期','', '### 7.1 PostgreSQL客服表','']
    lines+=md_table(['表','用途','owner/阶段','字段数','R','生命周期'],[[t['name'],t['title'],t['owner']+'/'+t['phase'],t['fields'],t['requirements'],t['lifecycle']] for t in pg_rows])
    lines+=['','### 7.2 Java MySQL增量','']
    lines+=md_table(['表','变更','owner/阶段','职责/保留'],[[t['name'],t['kind'],t['owner']+'/'+t['phase'],t['lifecycle']] for t in my_rows])
    lines+=['','### 7.3 既有事实表与外部存储','']
    lines+=md_table(['对象','责任','使用规则'],[
        ['pms_product / pms_sku_stock / pms_product_attribute*','M17/M18','MySQL可见性与实际SKU权威；ES仅召回'],
        ['oms_cart_item / oms_order / oms_order_item','M18','统一计价、资源锁和业务事务；Python不直接写'],
        ['ums_member / ums_member_receive_address / sms_coupon_history / 积分记录','M17/M18','复用现有服务，实际受影响表在S2迁移清单逐个核验'],
        ['langgraph_checkpoint schema','M04/M12/M20','锁定组件setup管理；检查点和写任务恢复分开；随删除擦除'],
        ['Milvus collection/generation','M09','派生索引，PG版本和授权复核为准；按维度隔离'],
        ['MinIO私有字节','M10/M08','PG元数据与Java保护引用协调；不放公网永久URL'],
        ['Redis / RabbitMQ','M14/M01','通知/限流/防重放与唤醒；不是消息或业务最终账本'],
        ['内存凭据代理','M01','仅易失Bearer；禁止进入数据库/MQ/检查点/模型'],
    ])
    lines+=['','原需求逻辑名ag_message_attachment由ag_attachment_ref的MESSAGE用途承接；不重复创建第二套关联表。ag_model_profile_version的secret_version_ref只供适配器，API读取历史不回传。业务审计、保护凭证和幂等账本不能跟普通聊天90天一起级联删除。','', '## 8 全量页面责任与状态','']
    lines+=md_table(['页面','名称/原型route','owner','模块','需求','接口数'],[[p['id'],p['title']+' / '+p['route'],p['owner'],p['modules'],p['requirements'],len(p['operations'])] for p in page_rows])
    lines+=['','### 8.1 页面实现状态规则','']
    lines+=md_table(['页面','核心状态与完成动作'],[
        ['B01','未登录/登录恢复/上下文无权；只恢复经过服务复核的商品或订单ID'],
        ['B02','空历史/加载/排队/生成/停止/中断/断网/人工等待/接待/删除；snapshot+SSE归并，不混control_epoch'],
        ['B03','真实SKU选择/无库存/无匹配/对比缺参数；跳商品详情，S2再准备加车'],
        ['B04','地址/券/积分选择、准备、过期、STALE、EXECUTING、UNKNOWN、SUCCEEDED/FAILED；模拟支付跳现有页面'],
        ['B05','多订单选择/无轨迹/模拟标记/明细退货资格/剩余数量/凭证/各售后阶段'],
        ['A01','等待/我的/历史/领取pending/409归属变化/离线/转交失败/关闭；备注独立，不先乐观接管'],
        ['A02','DRAFT/PARSING/PREVIEW_READY/INDEXING/READY/FAILED/RETIRED；质检后发布，失败旧版可用'],
        ['A03','草稿/未验证/可用/停用/版本冲突；秘密只显示就绪，历史不可变，用量未知可见'],
        ['A04','无权限/试算中/试算过期/执行/部分保护跳过/任务失败；清理需重新核验与确认'],
        ['A05','模拟物流新增/编辑、售后审核/收到货/模拟退款分按钮；版本冲突刷新，结果以Java为准'],
    ])
    lines+=['','## 9 设计差异与开发门槛','',
            '原114个operation保持，新增listModelProfileVersions与reconcileBridgeOperation。原25张PG表增加4张，MySQL新增表从7到9；字段、事件和页面版本语义同步。D03的168小时含边界和D04最大余数法仍为业务建议，S2前用边界样例固定。当前SQL与契约是未实施目标定义；已有数据库迁移须另建版本文件。','',
            'coverage.json可以定位某项需求的接口、表、页面、场景和责任；生成器以显式集合相等检查每个operation和PG/MySQL对象，无未归属与重复归属。该检查不证明运行时语义、数据库语法或业务验收，实施时仍需03/05分册的真实并发和恢复试验。']
    write('architecture/04-数据接口与页面承接.md','\n'.join(lines))
    print(json.dumps({'operations':len(ops),'pg_tables':len(tables),'mysql_new':len(mysql_names),'chapters':len(chapters),'scenarios':len(scenarios)},ensure_ascii=False))


if __name__=='__main__':
    generate()
