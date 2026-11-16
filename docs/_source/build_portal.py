"""Build the offline design review portal; reads docs only, never calls services."""
from pathlib import Path
import html
import json
import re

ROOT = Path(__file__).resolve().parents[1]
SECTIONS = [
    ("overview", "设计导航", "00-设计导航.md"),
    ("ui", "前端 UI", "01-前端UI设计.md"),
    ("database", "数据库", "02-数据库设计.md"),
    ("api", "接口契约", "03-接口设计.md"),
    ("trace", "需求映射", "04-需求设计映射.md"),
    ("decisions", "决策与环境", "05-设计决策与环境.md"),
    ("checks", "检查记录", "06-设计检查记录.md"),
    ("architecture", "系统架构总览", "07-系统架构与模块设计.md"),
    ("modules", "模块与工程", "architecture/01-模块与工程结构.md"),
    ("graph", "图编排与工具", "architecture/02-LangGraph与工具编排.md"),
    ("recovery", "流程与故障恢复", "architecture/03-关键流程与故障恢复.md"),
    ("ownership", "全量承接矩阵", "architecture/04-数据接口与页面承接.md"),
    ("operations", "部署运维与验收", "architecture/05-部署运维与验收.md"),
    ("plan", "第一阶段计划", "08-第一阶段开发与验收计划.md"),
    ("implementation", "骨架验收记录", "09-架构骨架实施与最小闭环验收.md"),
    ("models", "模型与依赖验收", "10-模型接入与依赖验收.md"),
    ("vision", "GPT 图片咨询验收", "11-GPT图片咨询闭环验收.md"),
    ("framework", "整体工程框架", "12-整体工程框架与模块落地清单.md"),
    ("runtime", "运行基础验收", "13-运行基础实施与验收.md"),
    ("knowledge", "知识闭环验收", "14-知识管理与引用闭环验收.md"),
    ("handoff", "人工接待验收", "15-人工接待闭环实施与验收.md"),
    ("cart", "加购确认验收", "16-加购确认与结果核对闭环验收.md"),
    ("order", "报价与下单验收", "17-复用Java计价与订单创建闭环验收.md"),
    ("pricing", "结算金额与质量收口", "18-结算金额修复与质量检查收口.md"),
]


def inline(text):
    text = html.escape(text)
    text = re.sub(r"&#x60;([^&#]+)&#x60;", r"<code>\1</code>", text)
    tick = chr(96)
    text = re.sub(tick + r"([^" + tick + r"]+)" + tick, r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    targets = {Path(file).name: key for key, _, file in SECTIONS}
    def link(match):
        label, target = match.groups()
        decoded = html.unescape(target)
        key = targets.get(decoded.split("/")[-1])
        if key:
            return '<a href="#' + key + '">' + label + '</a>'
        if re.match(r"^(https://|[\w\u4e00-\u9fff./-]+$)", decoded) and not decoded.startswith(".."):
            return '<a href="' + html.escape(decoded, quote=True) + '">' + label + '</a>'
        return label
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", link, text)
    return text


def markdown(source):
    lines = source.splitlines()
    out, toc = [], []
    i, heading = 0, 0
    fence = chr(96) * 3
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if line.startswith(fence):
            lang = line[3:]
            code = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(fence):
                code.append(lines[i])
                i += 1
            out.append('<pre aria-label="' + html.escape(lang) + '"><code>' + html.escape("\n".join(code)) + '</code></pre>')
            i += 1
            continue
        match = re.match(r"^(#{1,3}) (.+)$", line)
        if match:
            level, title = len(match[1]), match[2]
            heading += 1
            anchor = "heading-" + str(heading)
            out.append(f'<h{level} id="{anchor}">{inline(title)}</h{level}>')
            if level == 2:
                toc.append(f'<button data-anchor="{anchor}">{inline(title)}</button>')
            i += 1
            continue
        if line.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s:|-]+\|$", lines[i + 1].strip()):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = lines[i].strip().strip("|").split("|")
                if not all(re.fullmatch(r"[\s:-]+", c) for c in cells):
                    rows.append(cells)
                i += 1
            out.append('<div class="table-scroll"><table><thead><tr>' + ''.join("<th>" + inline(c.strip()) + "</th>" for c in rows[0]) + '</tr></thead><tbody>')
            out += ["<tr>" + "".join("<td>" + inline(c.strip()) + "</td>" for c in row) + "</tr>" for row in rows[1:]]
            out.append("</tbody></table></div>")
            continue
        if re.match(r"^(- |\d+\. )", line):
            ordered = bool(re.match(r"^\d", line))
            tag = "ol" if ordered else "ul"
            out.append("<" + tag + ">")
            while i < len(lines) and re.match(r"^(- |\d+\. )", lines[i].strip()):
                out.append("<li>" + inline(re.sub(r"^(- |\d+\. )", "", lines[i].strip())) + "</li>")
                i += 1
            out.append("</" + tag + ">")
            continue
        text = [line]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(r"^(#|\||- |" + fence + r"|\d+\. )", lines[i].strip()):
            text.append(lines[i].strip())
            i += 1
        out.append("<p>" + inline(" ".join(text)) + "</p>")
    return "\n".join(out), "".join(toc)


def build_er():
    """Draw each real FK in the proposed PG DDL, including deferred relationships."""
    tables = json.loads((ROOT / "database/table-catalog.json").read_text(encoding="utf-8"))
    sql = (ROOT / "database/postgresql-v1.sql").read_text(encoding="utf-8")
    names = {t["name"] for t in tables}
    edges = set()
    for t in tables:
        for n, typ, _ in t["fields"]:
            m = re.search(r"REFERENCES (\w+)\((\w+)\)", typ)
            if m:
                edges.add((m[1], t["name"], n, "||" if "NOT NULL" in typ else "|o", "o|" if "UNIQUE" in typ else "o{"))
        for c in t["constraints"]:
            m = re.search(r"FOREIGN KEY\s*\(([^)]+)\)\s*REFERENCES (\w+)\(([^)]+)\)", c)
            if m:
                edges.add((m[2], t["name"], m[1].replace(",", "_"), "|o", "o{"))
    for child, col, parent in re.findall(r"ALTER TABLE (\w+) ADD CONSTRAINT \w+\s+FOREIGN KEY\(([^)]+)\) REFERENCES (\w+)", sql):
        edges.add((parent, child, col.replace(",", "_"), "|o", "o{"))
    out = ["erDiagram"]
    for t in tables:
        out.append("    " + t["name"] + " {")
        for n, typ, _ in t["fields"]:
            if "PRIMARY KEY" in typ or "REFERENCES" in typ or n in ("state", "status", "active_version_id", "run_id"):
                dt = re.sub(r"\(.*", "", typ.split()[0])
                out.append("        " + dt + " " + n + (" PK" if "PRIMARY KEY" in typ else " FK" if "REFERENCES" in typ else ""))
        out.append("    }")
    for parent, child, col, left, right in sorted(edges):
        if parent in names and child in names:
            out.append("    " + parent + " " + left + "--" + right + " " + child + ' : "' + col + '"')
    out += [
        "    oms_agent_quote |o--o{ oms_agent_operation : quote_id",
        "    oms_order_item |o--o{ oms_order_return_apply : order_item_id",
        "    oms_after_sale_rule_version |o--o{ oms_order_return_apply : rule_version_id",
        "    oms_order_return_apply ||--o{ oms_return_proof_reference : return_apply_id",
        "    oms_order ||--o| oms_shipment : order_id",
        "    oms_shipment ||--o{ oms_shipment_event : shipment_id",
        "    oms_agent_audit {",
        "        bigint id PK",
        "    }",
    ]
    (ROOT / "database/er.mmd").write_text("\n".join(out) + "\n", encoding="utf-8")


def generate():
    nav, articles = [], []
    for key, title, file in SECTIONS:
        path = ROOT / file
        source = path.read_text(encoding="utf-8") if path.exists() else "# 检查记录\n\n检查尚未完成。"
        body, toc = markdown(source)
        if key == "api":
            body = '<div class="explorer"><div class="section-eyebrow">离线接口浏览器</div><h2>查找与查看契约</h2><p>按路径、名称或需求编号筛选，展开查看请求参数、响应及数据结构。</p><div class="api-filters"><input id="api-search" aria-label="搜索接口" placeholder="搜索：创建订单 / R09 / operationId"><select id="api-scope" aria-label="接口范围"><option value="">所有接口</option><option value="member">买家端</option><option value="admin">管理端</option><option value="bridge">Java 内部桥接</option></select></div><div id="api-count"></div><div id="api-list"></div></div>' + body
        articles.append(f'<section class="doc-section" data-section="{key}" hidden><div class="doc-tools"><span>设计 v0.2 / 需求基线 1.1</span><a href="{file}" download>下载本节 Markdown ↗</a></div><div class="doc-toc">{toc}</div><article>{body}</article></section>')
        nav.append(f'<a href="#{key}" data-nav="{key}">{title}</a>')
    spec = (ROOT / "api/openapi.json").read_text(encoding="utf-8").replace("</", "<\\/")
    document = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light"><title>Mall 智能体 · 设计中心</title><link rel="stylesheet" href="portal.css"></head>
<body><header class="portal-header"><a class="portal-brand" href="#overview"><b>M</b><span>Mall 设计中心<small>智能客服与购物助手</small></span></a><span class="version-tag">v0.2 · 架构评审稿</span><a class="launch" href="ui/index.html">打开交互原型 ↗</a></header>
<div class="portal-layout"><aside class="portal-sidebar"><div class="nav-label">DESIGN DOCUMENTS</div><nav aria-label="设计文档">""" + "".join(nav) + """</nav><div class="sidebar-bottom"><span>需求 · 设计 · 契约</span><p>统一保存于 docs<br>支持离线浏览</p></div></aside><main>
<section class="hero"><div><div class="section-eyebrow">MALL AGENT / DESIGN REVIEW</div><h1>让每一步服务，<br>都清晰可确认。</h1><p>从需求到页面、数据与接口。围绕真实商品与订单，设计有依据的回答、可靠的业务确认和顺畅的人工接待。</p><div class="hero-actions"><a class="launch" href="ui/index.html">体验买家与员工端 ↗</a><a class="text-link" href="商城智能体需求分析与技术设计.docx">查看原需求文档 ↓</a></div></div><div class="hero-note"><span>完整设计范围</span><strong>18<small> 项需求</small></strong><div>10 个逻辑页面 · 8 个原型入口<br>PostgreSQL 客服库 + MySQL 商城扩展<br>OpenAPI 3.1 + 持久流式事件</div><p>原型为示例；骨架及文字/图片咨询已实施，进度见验收记录。</p></div></section>
<div class="resource-grid"><a href="database/postgresql-v1.sql" download><small>DATABASE / 01</small><b>客服库 DDL</b><span>PostgreSQL 16 · 设计脚本 ↓</span></a><a href="database/mysql-v1.sql" download><small>DATABASE / 02</small><b>商城增量 DDL</b><span>MySQL 8.0.35 · 设计脚本 ↓</span></a><a href="api/openapi.json" download><small>API CONTRACT</small><b>OpenAPI 契约</b><span>请求、响应与权限 ↓</span></a></div>
""" + "".join(articles) + """<footer>本包以原需求基线为范围依据。页面与 SQL 为设计成果，业务验收在实现后进行。离线页以源码展示 Mermaid 图，支持 Mermaid 的 Markdown 阅读器可渲染。</footer></main></div><script id="api-spec" type="application/json">""" + spec + """</script><script src="portal.js"></script></body></html>"""
    (ROOT / "index.html").write_text(document, encoding="utf-8")
    build_er()
    return len(SECTIONS)


if __name__ == "__main__":
    print("Portal sections:", generate())
