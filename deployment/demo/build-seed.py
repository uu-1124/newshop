"""Build an allowlisted catalog/configuration seed from the tracked MySQL dump."""

import re
import sys
from pathlib import Path

KEEP = {
    "cms_prefrence_area_product_relation", "cms_prefrence_area", "cms_subject_category",
    "cms_subject_product_relation", "cms_subject", "oms_order_return_reason", "oms_order_setting",
    "pms_brand", "pms_member_price", "pms_product_attribute_category", "pms_product_attribute_value",
    "pms_product_attribute", "pms_product_category_attribute_relation", "pms_product_category",
    "pms_product_full_reduction", "pms_product_ladder", "pms_product", "pms_sku_stock",
    "sms_coupon_product_category_relation", "sms_coupon_product_relation", "sms_coupon",
    "sms_flash_promotion_product_relation", "sms_flash_promotion_session", "sms_flash_promotion",
    "sms_home_advertise", "sms_home_brand", "sms_home_new_product", "sms_home_recommend_product",
    "sms_home_recommend_subject", "ums_integration_consume_setting", "ums_member_level", "ums_menu",
    "ums_resource_category", "ums_resource", "ums_role", "ums_role_menu_relation",
    "ums_role_resource_relation",
}


def main():
    original = Path(sys.argv[1]).read_text(encoding="utf-8-sig")
    insert = re.compile(r"^INSERT INTO `([a-z_]+)` VALUES .*;\s*$")
    drop = re.compile(r"^DROP TABLE IF EXISTS `[a-z_]+`;$")
    create = re.compile(r"^CREATE TABLE `[a-z_]+`\s*\($")
    output = ["SET NAMES utf8mb4;", "SET FOREIGN_KEY_CHECKS = 0;"]
    in_schema = False
    found = set()
    for line in original.splitlines():
        if in_schema:
            output.append(line)
            in_schema = not line.startswith(") ENGINE")
        elif create.fullmatch(line):
            output.append(line)
            in_schema = True
        elif drop.fullmatch(line):
            output.append(line)
        elif match := insert.fullmatch(line):
            if match[1] in KEEP:
                found.add(match[1])
                line = line.replace("http://127.0.0.1:18080/demo-assets/", "/demo-assets/")
                output.append(line)
        elif line.startswith("INSERT "):
            raise ValueError("Unexpected INSERT format in the catalog dump")
    if in_schema or found != KEEP:
        raise ValueError(f"Catalog dump schema or retained tables changed ({sorted(KEEP - found)})")
    output.append(Path(__file__).with_name("seed.sql").read_text(encoding="utf-8"))
    output.append("SET FOREIGN_KEY_CHECKS = 1;")
    sys.stdout.buffer.write(("\n".join(output) + "\n").encode("utf-8"))


if __name__ == "__main__":
    main()
