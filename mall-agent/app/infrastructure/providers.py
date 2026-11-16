class FixtureDialogProvider:
    """Explicit integration adapter. No AI/model capability or billing is claimed."""

    async def answer(self, question, products):
        header = "【架构联调测试回复，非模型生成】\n"
        if not products:
            return header + "商城本次未返回可展示商品。你的消息已持久保存，可以刷新页面核对历史。"
        lines = [f"{item['name']}：¥{item['price_from']}（商品ID {item['id']}）" for item in products]
        return (
            header
            + "已通过商城身份校验并读取当前可见商品：\n"
            + "\n".join(lines)
            + "\n本轮仅查询，不执行加车、下单或售后操作。"
        )
