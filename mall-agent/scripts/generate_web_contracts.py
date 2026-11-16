"""Generate shared wire types from the reviewed OpenAPI; never activates a route."""

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def literal(value):
    return json.dumps(value, ensure_ascii=False)


def wire_type(schema):
    if "$ref" in schema:
        return schema["$ref"].split("/")[-1]
    for key, separator in (("oneOf", " | "), ("anyOf", " | "), ("allOf", " & ")):
        if key in schema:
            base = {k: v for k, v in schema.items() if k != key}
            variants = []
            for value in schema[key]:
                if base.get("properties") and not value.get("$ref"):
                    value = (
                        base
                        | value
                        | {
                            "properties": base["properties"] | value.get("properties", {}),
                            "required": list(
                                dict.fromkeys(base.get("required", []) + value.get("required", []))
                            ),
                        }
                    )
                variants.append(wire_type(value))
            return "(" + separator.join(variants) + ")"
    if "const" in schema:
        return literal(schema["const"])
    if "enum" in schema:
        return " | ".join(literal(value) for value in schema["enum"])
    kind = schema.get("type")
    if isinstance(kind, list):
        return " | ".join(wire_type(schema | {"type": value}) for value in kind)
    if kind == "array":
        return "Array<" + wire_type(schema.get("items", {})) + ">"
    if kind == "object" or "properties" in schema:
        required = set(schema.get("required", []))
        fields = [
            f"{literal(name)}{'' if name in required else '?'}: {wire_type(value)}"
            for name, value in schema.get("properties", {}).items()
        ]
        result = "{ " + "; ".join(fields) + " }" if fields else "Record<string, unknown>"
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict):
            result += " & Record<string, " + wire_type(extra) + ">"
        return result
    return {
        "string": "string",
        "integer": "number",
        "number": "number",
        "boolean": "boolean",
        "null": "null",
    }.get(kind, "unknown")


def generate():
    spec = json.loads((ROOT / "docs/api/openapi.json").read_text(encoding="utf-8"))
    lines = [
        "// Generated from docs/api/openapi.json by mall-agent/scripts/generate_web_contracts.py.",
        "// Wire types describe the full design; availability requires a real server capability.",
        "// Java IDs and monetary values remain strings according to the API schema.",
        "",
    ]
    for name, schema in sorted(spec["components"]["schemas"].items()):
        lines.append(f"export type {name} = {wire_type(schema)}")
    lines.extend(["", "export type AgentOperations = {"])
    for path, methods in spec["paths"].items():
        for method, operation in methods.items():
            if not isinstance(operation, dict) or "operationId" not in operation:
                continue
            request = (
                operation.get("requestBody", {})
                .get("content", {})
                .get("application/json", {})
                .get("schema", {})
            )
            response = (
                next(
                    (value for code, value in operation["responses"].items() if str(code).startswith("2")), {}
                )
                .get("content", {})
                .get("application/json", {})
                .get("schema", {})
            )
            lines.append(
                f"  {literal(operation['operationId'])}: {{ method: {literal(method.upper())}; "
                f"path: {literal(path)}; request: {wire_type(request)}; response: {wire_type(response)} }}"
            )
    lines.extend(["}", "", "export type AgentOperationId = keyof AgentOperations", ""])
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    content = generate()
    for relative in [
        "mall-app-web/src/services/agent-contracts.ts",
        "mall-admin-web/src/apis/agent-contracts.ts",
    ]:
        path = ROOT / relative
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                raise SystemExit(f"Stale web contract: {relative}")
        else:
            path.write_text(content, encoding="utf-8")
    print("PASS shared web contracts" + (" checked" if args.check else " generated"))
