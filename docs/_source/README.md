# 设计稿维护

此目录只生成文档、契约和原型检查结果，不连接数据库或服务器。原需求 DOCX 是只读输入，请勿覆盖。

维护顺序：

1. 修改 database_design.py、api_design.py、design_documents.py 中的设计定义；原型修改 ui/app.js 与 ui/styles.css。
2. 使用Python依次运行 database_design.py、api_design.py、design_documents.py、architecture_design.py，再运行 build_portal.py。
3. 使用Node运行 prototype_logic_check.cjs；使用Python依次运行 validate_design.py、validate_architecture.py。
4. 再运行 build_portal.py，把最新检查记录嵌入离线入口。

architecture_contracts.py集中维护v0.2对API/DDL/派生文档的架构补充；architecture_design.py维护显式模块归属并生成coverage.json与04承接矩阵。07总览、architecture的01/02/03/05分册及08计划为人工编写源文档，生成流程不覆盖它们。修改功能契约时同步调整所有映射，未归属operation或表会使生成失败。所有脚本离线读写docs，不连接服务器。

Python 需要 python-docx 读取原需求表格。本机已具备的随附 Python/Node 运行时位于 C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/ 下。运行 Python 时建议 -B 并设置 PYTHONIOENCODING=utf-8；Node 使用上述 node/bin/node.exe。

validate_design.py 使用已核验的需求文件 SHA256，需求正式升级后需人工更新基线与映射。原型检查使用存根，未替代浏览器验证；SQL 结构检查未替代隔离库执行与迁移验证。
