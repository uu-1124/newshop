"""Offline structural checks. Not a substitute for DB execution or API integration."""
from pathlib import Path
from collections import Counter
from html.parser import HTMLParser
from urllib.parse import unquote
import hashlib
import json
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_HASH = "EAAFDC3D0528572B9A7C337087927C85D01C2D10440928C5BDB79AC7D20081D3"
NODE = Path(r"C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe")
checks = []


def check(name, condition, detail):
    checks.append({"name": name, "passed": bool(condition), "detail": detail})


def read(path):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def walk(obj):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from walk(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from walk(value)


def resolve(ref, document):
    filename, _, fragment = ref.partition("#")
    target = read("api/" + filename) if filename else document
    for key in fragment.strip("/").split("/") if fragment else []:
        target = target[key.replace("~1", "/").replace("~0", "~")]
    return target


def clean_sql(source):
    """Remove quoted literals and line comments while retaining offsets and structure."""
    return re.sub(r"'(?:''|[^'])*'|--[^\n]*", lambda m: "''" if m[0].startswith("'") else "", source)


def split_sql_items(body):
    result, start, depth = [], 0, 0
    for i, char in enumerate(body):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            result.append(body[start:i].strip())
            start = i + 1
    result.append(body[start:].strip())
    return result


def parse_tables(source):
    source = clean_sql(source)
    tables = {}
    for match in re.finditer(r"CREATE TABLE\s+(?:IF NOT EXISTS\s+)?([\w.]+)\s*\(", source, re.I):
        depth, pos, start = 1, match.end(), match.end()
        while depth and pos < len(source):
            if source[pos] == "(":
                depth += 1
            elif source[pos] == ")":
                depth -= 1
            pos += 1
        items = split_sql_items(source[start:pos-1])
        fields = {}
        for item in items:
            m = re.match(r"(\w+)\s+(.+)", item, re.S)
            if m and m[1].upper() not in {"CONSTRAINT","PRIMARY","FOREIGN","UNIQUE","KEY","CHECK"}:
                fields[m[1]] = m[2]
        tables[match[1]] = {"fields": fields, "body": source[start:pos-1], "items": items}
    return tables


def schema_matches(value, schema, doc):
    """Subset used only for selected positive/negative contract examples."""
    if "$ref" in schema:
        return schema_matches(value, resolve(schema["$ref"], doc), doc)
    if "const" in schema and value != schema["const"]:
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if any(not schema_matches(value, s, doc) for s in schema.get("allOf", [])):
        return False
    if "oneOf" in schema and sum(schema_matches(value, s, doc) for s in schema["oneOf"]) != 1:
        return False
    if "anyOf" in schema and not any(schema_matches(value, s, doc) for s in schema["anyOf"]):
        return False
    if "not" in schema and schema_matches(value, schema["not"], doc):
        return False
    if "if" in schema:
        branch = schema.get("then", {}) if schema_matches(value, schema["if"], doc) else schema.get("else", {})
        if not schema_matches(value, branch, doc):
            return False
    kinds = schema.get("type", [])
    kinds = [kinds] if isinstance(kinds, str) else kinds
    if kinds and not any({"null": value is None, "object": isinstance(value,dict), "array": isinstance(value,list),
                          "string": isinstance(value,str), "integer": type(value) is int,
                          "number": type(value) in (int,float), "boolean": type(value) is bool}.get(k, False) for k in kinds):
        return False
    if isinstance(value, dict):
        if any(k not in value for k in schema.get("required", [])):
            return False
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False and any(k not in props for k in value):
            return False
        return all(schema_matches(v, props[k], doc) for k,v in value.items() if k in props)
    if isinstance(value, list):
        return len(value) >= schema.get("minItems", 0) and len(value) <= schema.get("maxItems", 10**9) and all(schema_matches(v, schema.get("items", {}), doc) for v in value)
    if isinstance(value, str):
        return len(value) >= schema.get("minLength", 0) and len(value) <= schema.get("maxLength", 10**9) and ("pattern" not in schema or re.search(schema["pattern"], value) is not None)
    if type(value) in (int,float):
        return value >= schema.get("minimum", -float("inf")) and value <= schema.get("maximum", float("inf"))
    return True


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths=[]
    def handle_starttag(self, tag, attrs):
        for k,v in attrs:
            if k in {"href","src"} and v and not v.startswith(("#","http:","https:","data:","blob:")):
                self.paths.append(unquote(v.split("#")[0]))


def validate():
    doc = read("api/openapi.json")
    events = read("api/sse-events.schema.json")
    tables = read("database/table-catalog.json")
    pages = read("ui/page-catalog.json")
    reqs = read("api/requirement-catalog.json")
    ops = [(p,m,o) for p,ms in doc["paths"].items() for m,o in ms.items() if "operationId" in o]
    ids = [o["operationId"] for _,_,o in ops]
    check("接口标识",len(ids)==len(set(ids))==len(read("api/operation-catalog.json")),f"{len(ids)} 个 operationId 唯一且与目录一致")
    ref_count, bad_refs = 0, []
    for name, data in [("openapi",doc),("events",events)]:
        for node in walk(data):
            if "$ref" in node:
                ref_count += 1
                try:
                    resolve(node["$ref"], data)
                except (KeyError,FileNotFoundError,TypeError):
                    bad_refs.append(node["$ref"])
    check("Schema 引用", not bad_refs, f"解析 {ref_count} 个本地/事件跨文件引用；错误 {bad_refs}")
    bad_paths, bad_idem, bad_auth, bad_mapping = [], [], [], []
    valid_req = {r["id"] for r in reqs}
    valid_pages = {p["id"] for p in pages}
    for path, method, op in ops:
        ps=[resolve(p["$ref"],doc) if "$ref" in p else p for p in op.get("parameters",[])]
        actual={p["name"] for p in ps if p["in"]=="path" and p.get("required")}
        if actual != set(re.findall(r"\{(\w+)\}",path)):
            bad_paths.append(path)
        if method in {"post","put","patch","delete"} and not any(p["name"]=="Idempotency-Key" and p.get("required") for p in ps):
            bad_idem.append(op["operationId"])
        if not op.get("security"):
            bad_auth.append(op["operationId"])
        if "agent-bridge" in path:
            allowed = ([{"BridgeSignature": [], "OperationReceipt": []}] if op["operationId"] == "reconcileBridgeOperation" else None)
            if allowed is not None and (op["security"] != allowed or not op.get("x-internal-reconciliation-only")):
                bad_auth.append(op["operationId"])
            elif allowed is None and not all("BridgeSignature" in s and ("MemberBearer" in s or "StaffBearer" in s) for s in op["security"]):
                bad_auth.append(op["operationId"])
        if not set(op["x-requirements"]).issubset(valid_req) or not set(op["x-ui-pages"]).issubset(valid_pages):
            bad_mapping.append(op["operationId"])
    check("路径参数",not bad_paths,f"所有路径占位符都有必填参数；错误 {bad_paths}")
    check("写请求幂等",not bad_idem,f"{sum(m in {'post','put','patch','delete'} for _,m,_ in ops)} 个写请求含必填 Idempotency-Key")
    check("入口鉴权",not bad_auth,f"所有入口有身份要求；{sum('agent-bridge' in p for p,_,_ in ops)} 个内部桥接，普通22个要求主体令牌+服务签名，唯一核对例外要求receipt+签名")
    check("映射有效性",not bad_mapping,f"{len(pages)} 个逻辑页面、{len(reqs)} 个需求，接口引用均有效")
    missing=[]
    mysql_req={"R04":["pms_product","pms_sku_stock","pms_product_attribute_value"],
               "R08":["oms_agent_quote","oms_agent_operation","oms_cart_item"],"R09":["oms_agent_quote","oms_order"],
               "R10":["oms_order","oms_order_item","oms_shipment","oms_shipment_event"],
               "R11":["oms_agent_operation"],"R12":["oms_order_return_apply","oms_return_proof_reference"],
               "R18":["oms_agent_operation","oms_agent_audit"]}
    for rid in valid_req:
        if not any(rid in p["requirements"].split() for p in pages) or not any(rid in o["x-requirements"] for _,_,o in ops) or not (any(rid in t["requirements"] for t in tables) or mysql_req.get(rid)):
            missing.append(rid)
    check("需求覆盖",not missing,f"R01–R18 均有页面、数据和接口承接；缺失 {missing}")
    required_errors=[]
    for name, schema in doc["components"]["schemas"].items():
        if "properties" in schema:
            required_errors += [name+"."+k for k in schema.get("required",[]) if k not in schema["properties"]]
    check("对象必填字段",not required_errors,f"{len(doc['components']['schemas'])} 个 Schema；必填字段均存在")
    s=doc["components"]["schemas"]
    sample_errors=[]
    approve={"expected_version":1,"decision":"APPROVE","note":"核验通过","approved_amount":"12.30","company_address_id":"1"}
    reject={"expected_version":1,"decision":"REJECT","note":"不符合规则"}
    cases=[("批准完整",approve,True),("批准缺金额",{k:v for k,v in approve.items() if k!="approved_amount"},False),
           ("拒绝完整",reject,True),("拒绝携带批准金额",{**reject,"approved_amount":"12.30"},False)]
    for name, val, expected in cases:
        if schema_matches(val,s["ReturnReviewInput"],doc)!=expected:
            sample_errors.append(name)
    # Conditional CART/DIRECT rules tested at the branch layer, separately from generic fields.
    for val, expected in [({"source":"DIRECT","items":[{}]},True),({"source":"DIRECT","items":[]},False),
                          ({"source":"DIRECT","items":[{}],"cart_item_ids":["1"]},False),
                          ({"source":"CART","cart_item_ids":["1"]},True),({"source":"CART"},False)]:
        if schema_matches(val,{"allOf":s["CreateOrderParameters"]["allOf"]},doc)!=expected:
            sample_errors.append("下单互斥 "+str(val))
    for val, expected in [({"change_note":"演示","source_text":"规则内容"},True),({"change_note":"演示"},False),
                          ({"change_note":"演示","source_text":""},False),
                          ({"change_note":"演示","source_text":"规则","source_attachment_id":"id"},False)]:
        if schema_matches(val,s["KBVersionInput"],doc)!=expected:
            sample_errors.append("知识输入 "+str(val))
    check("关键条件契约样例",not sample_errors,f"13 个正/反例核对批准/拒绝、CART/DIRECT 及知识输入互斥；错误 {sample_errors}；仅针对所用 Schema 子集")
    pg=(ROOT/"database/postgresql-v1.sql").read_text(encoding="utf-8")
    my=(ROOT/"database/mysql-v1.sql").read_text(encoding="utf-8")
    parsed_pg,parsed_my=parse_tables(pg),parse_tables(my)
    check("DDL 表目录",set(parsed_pg)=={t["name"] for t in tables} and len(parsed_my)==9,f"PostgreSQL {len(parsed_pg)} 张、MySQL 新增 {len(parsed_my)} 张及 1 张扩展")
    balance_errors=[]
    for label,source in [("PostgreSQL",pg),("MySQL",my)]:
        depth=0
        for char in clean_sql(source):
            depth += (char=="(")-(char==")")
            if depth<0:
                balance_errors.append(label)
                break
        if depth!=0:
            balance_errors.append(label)
    check("DDL 结构括号",not balance_errors,f"字符串/行注释剔除后括号平衡；{balance_errors}；不等价于数据库语法执行")
    fk_errors=[]
    # Existing mall DDL backticks are normalized for structural column lookup.
    base=parse_tables((ROOT.parent/"mall-swarm/document/sql/mall.sql").read_text(encoding="utf-8").replace(chr(96),""))
    missing_business_tables=[name for names in mysql_req.values() for name in names if name not in {**base,**parsed_my}]
    if missing_business_tables:
        fk_errors.extend(missing_business_tables)
    for label,source,lookup in [("PG",pg,parsed_pg),("MySQL",my,{**base,**parsed_my})]:
        for target, cols in re.findall(r"REFERENCES (\w+)\s*\(([^)]+)\)",clean_sql(source),re.I):
            if target not in lookup or any(c.strip() not in lookup[target]["fields"] for c in cols.split(",")):
                fk_errors.append(label+":"+target+"("+cols+")")
    check("外键目标",not fk_errors,f"PostgreSQL 自有表及 MySQL 新旧表外键目标列存在；错误 {fk_errors}")
    index_errors=[]
    for label,source in [("PG",pg),("MySQL",my)]:
        if label=="PG":
            ix=re.findall(r"CREATE (?:UNIQUE )?INDEX (\w+) ON (\w+)\s*\(([^)]+)\)",source)
            if len({x[0] for x in ix})!=len(ix):
                index_errors.append("PG索引名重复")
            for name,table,cols in ix:
                if table not in parsed_pg or any(c.strip().split()[0] not in parsed_pg[table]["fields"] for c in cols.split(",")):
                    index_errors.append(name)
    enum_errors=[]
    for t in tables:
        for n,typ,desc in t["fields"]:
            length=re.search(r"varchar\((\d+)\)",typ)
            if length and desc.startswith("枚举：") and max(map(len,desc[3:].split()))>int(length[1]):
                enum_errors.append(t["name"]+"."+n)
    check("索引与枚举宽度",not index_errors and not enum_errors,f"PG 索引目标及名称、枚举字段宽度一致；错误 {index_errors+enum_errors}")
    link_errors=[]
    for file in [ROOT/"index.html",ROOT/"ui/index.html"]:
        if not file.exists():
            link_errors.append(str(file))
            continue
        parser=Links()
        parser.feed(file.read_text(encoding="utf-8"))
        link_errors += [str(file.relative_to(ROOT))+":"+p for p in parser.paths if not (file.parent/p).exists()]
    check("离线导航资源",not link_errors,f"入口 HTML 的本地 href/src 均存在；错误 {link_errors}")
    script_errors=[]
    for script in [ROOT/"portal.js",ROOT/"ui/app.js"]:
        result=subprocess.run([str(NODE),"--check",str(script)],capture_output=True,text=True)
        if result.returncode:
            script_errors.append(result.stderr)
    check("原型脚本语法",not script_errors,"Node --check 核对 portal.js 与 ui/app.js")
    checksum=hashlib.sha256((ROOT/"商城智能体需求分析与技术设计.docx").read_bytes()).hexdigest().upper()
    check("原需求保持原样",checksum==EXPECTED_HASH,"SHA256 "+checksum)
    report={"date":"2026-11-11","kind":"offline-structural-review","checks":checks,
            "passed":all(c["passed"] for c in checks),
            "limits":["未使用完整 OpenAPI 元模型校验器","未执行 SQL、建库或迁移","原型状态演示不等于业务功能验收"]}
    (ROOT/"design-validation.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    lines=["# 设计检查记录","","日期：2026-11-11。检查对象为设计文档与离线原型，架构v0.2。","","## 静态一致性","",
           "| 检查 | 结果 | 依据 |","|---|---|---|"]
    lines += ["| "+c["name"]+" | "+("通过" if c["passed"] else "待修复")+" | "+c["detail"]+" |" for c in checks]
    prototype=ROOT/"prototype-validation.json"
    if prototype.exists():
        pr=json.loads(prototype.read_text(encoding="utf-8"))
        lines += ["","## 原型逻辑检查","","使用 Node VM 和最小 DOM 存根检查原型状态与生成的 HTML 字符串，不启动浏览器。","",
                  "| 检查 | 结果 |","|---|---|"]
        lines += ["| "+c["name"]+" | "+("通过" if c["passed"] else "待修复")+" |" for c in pr["checks"]]
        lines += ["","机器结果见 prototype-validation.json，可运行 _source/prototype_logic_check.cjs 复查。"]
    lines += ["","## 检查边界","","上述结果为结构和选定规则校验，不代表完整 OpenAPI 官方符合性、SQL 在目标数据库执行通过或真实业务验收。SQL 需要后续在隔离库验证语法、迁移、并发与恢复；接口需要实现后联调。没有启动数据库、调用模型、变更云端容器或修改商城应用。","",
              "可使用 _source/validate_design.py 复查；机器结果见 design-validation.json。","","## 原型浏览检查",""]
    browser=ROOT/"_source/browser-review.md"
    lines.append(browser.read_text(encoding="utf-8") if browser.exists() else "尚未完成浏览器检查。")
    (ROOT/"06-设计检查记录.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    print(json.dumps({"passed":report["passed"],"checks":len(checks),"failures":[c for c in checks if not c["passed"]]},ensure_ascii=False))
    return report["passed"]


if __name__=="__main__":
    sys.exit(0 if validate() else 1)
