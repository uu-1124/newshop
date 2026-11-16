"""Build a concrete OpenAPI 3.1 design contract; no network or application writes."""
from pathlib import Path
import json, re

ROOT=Path(__file__).resolve().parents[1]
S={}
PATHS={}
OPS=[]

def ref(n): return {"$ref":"#/components/schemas/"+n}
def obj(p,required=(),description=None):
    d={"type":"object","properties":p,"additionalProperties":False}
    if required: d["required"]=list(required)
    if description: d["description"]=description
    return d
def st(maxn=500,**kw): return dict(type="string",maxLength=maxn,**kw)
def en(*values): return {"type":"string","enum":list(values)}
def arr(item,maxn=100): return {"type":"array","items":item,"maxItems":maxn}
def integer(lo=0,hi=2147483647): return {"type":"integer","minimum":lo,"maximum":hi}
def nullable(x): return {"anyOf":[x,{"type":"null"}]}
def schema(n,p,required=(),description=None): S[n]=obj(p,required,description); return ref(n)
def page(n,item):
    return schema(n,{"items":arr(item),"next_cursor":nullable(st(2048)),"has_more":{"type":"boolean"}},["items","next_cursor","has_more"])

UUID={"type":"string","format":"uuid"}
JID={"type":"string","pattern":"^[1-9][0-9]{0,18}$","description":"Java bigint ID，以十进制字符串传输"}
SEQ={"type":"string","pattern":"^(0|[1-9][0-9]*)$","description":"大整数序号，以字符串传输"}
TIME={"type":"string","format":"date-time"}
MONEY={"type":"string","pattern":"^(0|[1-9][0-9]{0,15})\\.[0-9]{2}$","examples":["2999.00"]}
BOOL={"type":"boolean"}
EXPECTED={"type":"integer","minimum":1,"description":"客户端最后读取的对象版本"}
CTYPES=["ADD_CART","CREATE_ORDER","CANCEL_ORDER","CREATE_RETURN"]
ASTATES=["PENDING_CONFIRMATION","EXECUTING","SUCCEEDED","FAILED","UNKNOWN","CANCELLED","EXPIRED","STALE"]
RSTATES=["QUEUED","RUNNING","COMPLETED","STOP_REQUESTED","STOPPED","FAILED","INTERRUPTED"]
CSTATES=["AI_ACTIVE","WAITING_HUMAN","HUMAN_ACTIVE","CLOSED"]

schema("Empty",{})
schema("VersionInput",{"expected_version":EXPECTED},["expected_version"])
schema("ErrorDetail",{"reason":st(100),"retry_after_seconds":integer(),"current_version":EXPECTED,"resource_id":st(120),
                     "field_errors":arr(obj({"field":st(100),"message":st(200)},["field","message"]))})
schema("ErrorEnvelope",{"code":st(100),"message":st(500),"data":nullable(ref("ErrorDetail")),"request_id":UUID},
       ["code","message","data","request_id"])
schema("EntryContext",{"kind":en("GENERAL","PRODUCT","ORDER"),"product_id":JID,"order_id":JID},["kind"],
       "PRODUCT/ORDER 分别必须提供对应 ID；GENERAL 不带业务 ID；服务端从 Java 核验对象")
schema("Identity",{"id":JID,"kind":en("MEMBER","STAFF"),"display_name":st(80),"permissions":arr(st(100),100),
                   "token_valid_until":nullable(TIME)},["id","kind","display_name","permissions","token_valid_until"])
schema("Conversation",{"id":UUID,"title":st(100),"state":en(*CSTATES),"waiting_ai_enabled":BOOL,
    "assigned_staff":nullable(obj({"id":JID,"display_name":st(80),"presence":en("ONLINE","BUSY","OFFLINE")},["id","display_name","presence"])),
    "entry_context":ref("EntryContext"),"last_message_preview":st(200),"unread_count":integer(),"version":EXPECTED,
    "created_at":TIME,"updated_at":TIME,"closed_at":nullable(TIME)},
    ["id","title","state","waiting_ai_enabled","assigned_staff","entry_context","last_message_preview","unread_count","version","created_at","updated_at","closed_at"])
schema("ConversationCreate",{"title":st(100),"entry_context":ref("EntryContext")},["entry_context"])
schema("ConversationRename",{"title":st(100,minLength=1),"expected_version":EXPECTED},["title","expected_version"])
schema("ReadInput",{"last_seq":SEQ},["last_seq"])
schema("ReadPosition",{"last_seq":SEQ,"unread_count":integer()},["last_seq","unread_count"])
schema("SourceCitation",{"document_id":UUID,"version_id":UUID,"chunk_id":UUID,"title":st(200),"section":st(500),
    "page_start":nullable(integer(1)),"page_end":nullable(integer(1)),"available":BOOL},
    ["document_id","version_id","chunk_id","title","section","page_start","page_end","available"])
schema("Sku",{"id":JID,"attributes":arr(obj({"name":st(80),"value":st(100)},["name","value"]),20),
    "price":MONEY,"available_quantity":integer(),"visible":BOOL},["id","attributes","price","available_quantity","visible"])
schema("Product",{"id":JID,"name":st(200),"brand":st(100),"category_id":JID,"price_from":MONEY,
    "image_url":st(2048),"detail_path":st(500),"sku_options":arr(ref("Sku"),100),"facts":arr(obj({"name":st(80),"value":st(500)},["name","value"]),40),
    "checked_at":TIME},["id","name","brand","category_id","price_from","image_url","detail_path","sku_options","facts","checked_at"])
schema("OrderLine",{"order_item_id":JID,"product_id":JID,"sku_id":JID,"name":st(200),"specification":st(300),
    "quantity":integer(1,999),"unit_price":MONEY,"paid_line_amount":MONEY,"returnable_quantity":integer(0,999)},
    ["order_item_id","product_id","sku_id","name","specification","quantity","unit_price","paid_line_amount","returnable_quantity"])
schema("ReturnSummary",{"id":JID,"order_item_id":nullable(JID),"quantity":integer(1,999),"requested_amount":MONEY,
    "approved_amount":nullable(MONEY),"stage":en("LEGACY","REQUESTED","APPROVED","GOODS_RECEIVED","SIMULATED_REFUNDED","REJECTED"),
    "legacy_status":nullable(integer(0,3)),"reason":st(500),"version":EXPECTED,"is_simulated":BOOL,
    "created_at":TIME,"updated_at":TIME},
    ["id","order_item_id","quantity","requested_amount","approved_amount","stage","legacy_status","reason","version","is_simulated","created_at","updated_at"])
schema("ReturnAddress",{"id":JID,"label":st(500)},["id","label"])
schema("ReturnProof",{"attachment_id":UUID,"sha256":st(64),"mime_type":st(100),"byte_size":integer(1,5242880)},["attachment_id","sha256","mime_type","byte_size"])
S["ReturnSummary"]["properties"].update({"order_id":JID,"description":st(1000),"proofs":arr(ref("ReturnProof"),5),"company_address":nullable(ref("ReturnAddress")),"handle_note":nullable(st(1000)),"receive_note":nullable(st(1000)),"simulated_refund_no":nullable(UUID)})
S["ReturnSummary"]["properties"]["rule_version"] = nullable(st(80))
schema("ReturnRule",{"id":JID,"version_no":integer(1),"rule_version":st(80),"return_window_hours":integer(1,720),"allow_shipped":BOOL,"amount_basis":{"const":"PAID_ITEM_NET"},"is_simulated":{"const":True},"effective_at":TIME,"note":st(500),"created_by":SEQ,"created_at":TIME},["id","version_no","rule_version","return_window_hours","allow_shipped","amount_basis","is_simulated","effective_at","note","created_by","created_at"])
schema("ReturnRulePage",{"items":arr(ref("ReturnRule"),100),"active_rule_version":st(80)},["items","active_rule_version"])
schema("ReturnRuleInput",{"expected_latest_version":integer(1,2147483646),"return_window_hours":integer(1,720),"allow_shipped":BOOL,"effective_at":TIME,"note":st(500,minLength=1)},["expected_latest_version","return_window_hours","allow_shipped","effective_at","note"])
schema("OrderSummary",{"id":JID,"order_sn":st(80),"status":integer(0,5),"status_label":st(40),"pay_amount":MONEY,
    "created_at":TIME,"receive_time":nullable(TIME),"address_summary":st(300),"lines":arr(ref("OrderLine"),100),
    "returns":arr(ref("ReturnSummary"),100),"detail_path":st(500),"payment_path":nullable(st(500)),"is_simulated":BOOL},
    ["id","order_sn","status","status_label","pay_amount","created_at","receive_time","address_summary","lines","returns","detail_path","payment_path","is_simulated"])
schema("LogisticsNode",{"id":JID,"occurred_at":TIME,"location":st(200),"description":st(1000),
    "version":EXPECTED,"is_simulated":{"const":True}},["id","occurred_at","location","description","version","is_simulated"])
schema("Logistics",{"order_id":JID,"shipment_id":nullable(JID),"carrier":nullable(st(100)),"tracking_no":nullable(st(100)),
    "nodes":arr(ref("LogisticsNode"),200),"version":EXPECTED,"is_simulated":{"const":True}},
    ["order_id","shipment_id","carrier","tracking_no","nodes","version","is_simulated"])
schema("AddressOption",{"id":JID,"label":st(300),"is_default":BOOL},["id","label","is_default"])
schema("CouponOption",{"id":JID,"name":st(160),"amount":MONEY,"available":BOOL,"unavailable_reason":nullable(st(300))},
    ["id","name","amount","available","unavailable_reason"])
schema("CheckoutSelection",{"action_type":{"const":"CREATE_ORDER"},"source":en("CART","DIRECT"),"cart_item_ids":arr(JID,20),"items":arr(ref("SelectedItem"),20)},["action_type","source"],"购物车或直接购买的只读结算选项；DIRECT不临时写购物车。")
S["CheckoutSelection"]["allOf"]=[{"if":{"properties":{"source":{"const":"DIRECT"}}},"then":{"required":["items"],"properties":{"items":{"minItems":1}},"not":{"required":["cart_item_ids"]}}},{"if":{"properties":{"source":{"const":"CART"}}},"then":{"required":["cart_item_ids"],"properties":{"cart_item_ids":{"minItems":1}},"not":{"required":["items"]}}}]
schema("CheckoutOptions",{"addresses":arr(ref("AddressOption")),"coupons":arr(ref("CouponOption")),
    "available_points":integer(),"points_rule_description":st(500),"manage_address_path":st(500)},
    ["addresses","coupons","available_points","points_rule_description","manage_address_path"])
schema("SelectedItem",{"product_id":JID,"sku_id":JID,"quantity":integer(1,999)},["product_id","sku_id","quantity"])
schema("CartSnapshotItem",{"id":JID,"product_id":nullable(JID),"sku_id":nullable(JID),"name":st(500),
    "specification":st(300),"quantity":integer(0,2147483647),"available":BOOL,
    "available_quantity":integer(0,2147483647),"unit_price":nullable(MONEY)},
    ["id","product_id","sku_id","name","specification","quantity","available","available_quantity","unit_price"])
schema("CartSnapshot",{"items":arr(ref("CartSnapshotItem"),20),"next_cursor":nullable(JID),"has_more":BOOL,"checked_at":TIME},
    ["items","next_cursor","has_more","checked_at"],"当前会员购物车的只读分页快照；价格、库存须在报价与确认时重新校验，不返回会员编号。")
schema("AddCartParameters",{"action_type":{"const":"ADD_CART"},"items":arr(ref("SelectedItem"),20)},["action_type","items"])
S["AddCartParameters"]["properties"]["items"]["minItems"]=1
schema("CreateOrderParameters",{"action_type":{"const":"CREATE_ORDER"},"source":en("DIRECT","CART"),
    "items":arr(ref("SelectedItem"),20),"cart_item_ids":arr(JID,20),"address_id":JID,
    "coupon_id":nullable(JID),"use_points":integer(0,100000000),"note":st(300)},
    ["action_type","source","address_id","coupon_id","use_points"],
    "DIRECT 必须 items 且不带 cart_item_ids；CART 必须 cart_item_ids 且不带 items；服务端核验归属。DIRECT 不临时修改购物车。")
S["CreateOrderParameters"]["allOf"]=[
    {"if":{"properties":{"source":{"const":"DIRECT"}}},"then":{"required":["items"],"properties":{"items":{"minItems":1}},"not":{"required":["cart_item_ids"]}}},
    {"if":{"properties":{"source":{"const":"CART"}}},"then":{"required":["cart_item_ids"],"properties":{"cart_item_ids":{"minItems":1}},"not":{"required":["items"]}}}
]
schema("CancelOrderParameters",{"action_type":{"const":"CANCEL_ORDER"},"order_id":JID},["action_type","order_id"])
schema("CreateReturnParameters",{"action_type":{"const":"CREATE_RETURN"},"order_id":JID,"order_item_id":JID,
    "quantity":integer(1,999),"reason_code":st(80),"description":st(1000),"attachment_ids":arr(UUID,5)},
    ["action_type","order_id","order_item_id","quantity","reason_code","description","attachment_ids"])
S["ActionParameters"]={"oneOf":[ref(x+"Parameters") for x in ["AddCart","CreateOrder","CancelOrder","CreateReturn"]],
    "discriminator":{"propertyName":"action_type","mapping":dict(zip(CTYPES,["#/components/schemas/"+x+"Parameters" for x in ["AddCart","CreateOrder","CancelOrder","CreateReturn"]]))}}
schema("Pricing",{"subtotal":MONEY,"promotion_discount":MONEY,"coupon_discount":MONEY,"points_discount":MONEY,
    "freight":MONEY,"payable":MONEY,"currency":{"const":"CNY"}},["subtotal","promotion_discount","coupon_discount","points_discount","freight","payable","currency"])
schema("CancellationSummary",{"id":JID,"order_sn":st(80),"pay_amount":MONEY,"status":{"const":0}},
    ["id","order_sn","pay_amount","status"],"取消确认的权威订单摘要，必须与参数中的order_id和报价金额一致；不含执行授权。")
schema("Quote",{"quote_id":UUID,"parameters":ref("ActionParameters"),"parameters_hash":st(64,pattern="^[0-9a-f]{64}$"),
    "lines":arr(obj({"product_id":JID,"sku_id":JID,"name":st(200),"specification":st(300),"quantity":integer(1,999),"unit_price":MONEY,
                    "line_payable":MONEY},["product_id","sku_id","name","specification","quantity","unit_price","line_payable"]),20),
    "pricing":ref("Pricing"),"address_summary":nullable(st(300)),"rule_version":st(80),"expires_at":TIME,
    "cancellation":ref("CancellationSummary")},
    ["quote_id","parameters","parameters_hash","lines","pricing","address_summary","rule_version","expires_at"])
schema("BusinessResult",{"operation_no":UUID,"state":en("SUCCEEDED","REJECTED","NOT_COMMITTED"),
    "business_type":nullable(en("CART","ORDER","ORDER_CANCEL","RETURN_APPLY")),"business_id":nullable(JID),
    "error_code":nullable(st(80)),"message":st(500),"order":nullable(ref("OrderSummary")),"return_apply":nullable(ref("ReturnSummary")),
    "cart_quantity":nullable(integer()),"completed_at":nullable(TIME)},
    ["operation_no","state","business_type","business_id","error_code","message","order","return_apply","cart_quantity","completed_at"],
    "NOT_COMMITTED 只表示当前没有已提交结果，不能判定失败或生成新操作编号。")
schema("Action",{"id":UUID,"conversation_id":UUID,"action_type":en(*CTYPES),"state":en(*ASTATES),
    "version":EXPECTED,"conversation_version":EXPECTED,"parameters":ref("ActionParameters"),"quote":ref("Quote"),
    "expires_at":TIME,"result":nullable(ref("BusinessResult")),"error_code":nullable(st(80))},
    ["id","conversation_id","action_type","state","version","conversation_version","parameters","quote","expires_at","result","error_code"])
schema("ActionPrepareInput",{"expected_conversation_version":EXPECTED,"parameters":ref("ActionParameters")},
    ["expected_conversation_version","parameters"])
schema("Attachment",{"id":UUID,"name":st(255),"mime_type":st(100),"byte_size":integer(1,20971520),
    "purpose":en("CHAT_IMAGE","KNOWLEDGE","RECORDING","SPEECH"),"state":en("UPLOADING","READY","FAILED","DELETING","DELETED"),
    "protected":BOOL,"retention_until":nullable(TIME)},["id","name","mime_type","byte_size","purpose","state","protected","retention_until"])
schema("StaffOrderSummary",{"id":JID,"order_sn":st(80),"status":integer(0,5),"status_label":st(40),"pay_amount":nullable(MONEY),"created_at":TIME,
    "lines":arr(obj({"name":st(200),"specification":st(500),"quantity":integer(1),"unit_price":nullable(MONEY)},["name","specification","quantity","unit_price"]),100)},
    ["id","order_sn","status","status_label","pay_amount","created_at","lines"],"人工分享的只读订单摘要，已核验归属；不包含地址、电话、支付入口或可执行动作。")
S["MessageBlock"]={"oneOf":[
    obj({"type":{"const":"product"},"product":ref("Product")},["type","product"]),
    obj({"type":{"const":"order"},"order":ref("OrderSummary")},["type","order"]),
    obj({"type":{"const":"staff_order"},"staff_order":ref("StaffOrderSummary")},["type","staff_order"]),
    obj({"type":{"const":"action"},"action":ref("Action")},["type","action"]),
    obj({"type":{"const":"source"},"source":ref("SourceCitation")},["type","source"]),
    obj({"type":{"const":"attachment"},"attachment":ref("Attachment")},["type","attachment"]),
    obj({"type":{"const":"logistics"},"logistics":ref("Logistics")},["type","logistics"])]}
schema("ReplyFailure",{"code":st(80),"message":st(500),"retry_after":nullable(TIME)},["code","message","retry_after"],
    "已授权失败/中断回复的受控恢复说明；不包含供应商或驱动原始异常。retry_after为每日UTC额度窗口的下次起点，不承诺供应商届时可用。")
schema("Message",{"id":UUID,"conversation_id":UUID,"seq":SEQ,"sender_kind":en("MEMBER","ASSISTANT","STAFF","SYSTEM"),
    "sender_display_name":st(80),"client_message_id":nullable(UUID),"status":en("ACCEPTED","GENERATING","COMPLETED","STOPPED","FAILED","INTERRUPTED"),
    "text":st(100000),"blocks":arr(ref("MessageBlock"),100),"run_id":nullable(UUID),"reply_failure":nullable(ref("ReplyFailure")),"created_at":TIME},
    ["id","conversation_id","seq","sender_kind","sender_display_name","client_message_id","status","text","blocks","run_id","created_at"])
schema("MessageInput",{"client_message_id":UUID,"text":st(10000),"attachment_ids":arr(UUID,5)},
    ["client_message_id","text","attachment_ids"],"文字和附件不能同时为空；人工模式仅入库并通知，不启动 AI。")
schema("Run",{"id":UUID,"conversation_id":UUID,"trigger_message_id":UUID,"state":en(*RSTATES),
    "attempt":integer(1),"queue_position":nullable(integer(1)),"version":EXPECTED,"error_code":nullable(st(80)),
    "created_at":TIME,"started_at":nullable(TIME),"finished_at":nullable(TIME)},
    ["id","conversation_id","trigger_message_id","state","attempt","queue_position","version","error_code","created_at","started_at","finished_at"])
schema("MessageAccepted",{"message":ref("Message"),"run":nullable(ref("Run")),"event_cursor":SEQ},["message","run","event_cursor"])
schema("Snapshot",{"conversation":ref("Conversation"),"latest_seq":SEQ,"event_cursor":SEQ,
    "active_run":nullable(ref("Run")),"pending_actions":arr(ref("Action"),20)},["conversation","latest_seq","event_cursor","active_run","pending_actions"],
    "快照与事件高水位在同一一致性事务读取，订阅 after=event_cursor 后不会丢失窗口中的变更。")
schema("Handoff",{"id":UUID,"conversation_id":UUID,"state":en("WAITING","CLAIMED","WITHDRAWN","CLOSED"),
    "version":EXPECTED,"waiting_ai_enabled":BOOL,"staff_id":nullable(JID),"created_at":TIME},
    ["id","conversation_id","state","version","waiting_ai_enabled","staff_id","created_at"])
schema("HandoffInput",{"expected_conversation_version":EXPECTED,"reason":st(500)},["expected_conversation_version"])
schema("WaitingAIInput",{"expected_version":EXPECTED,"enabled":BOOL},["expected_version","enabled"])
schema("TransferInput",{"expected_version":EXPECTED,"target_staff_id":JID,"reason":st(500,minLength=1)},
    ["expected_version","target_staff_id","reason"])
schema("CloseInput",{"expected_version":EXPECTED,"reason":st(500,minLength=1)},["expected_version","reason"])
schema("PresenceInput",{"presence":en("ONLINE","BUSY","OFFLINE"),"expected_version":EXPECTED},["presence","expected_version"])
schema("Staff",{"id":JID,"display_name":st(80),"presence":en("ONLINE","BUSY","OFFLINE"),"capacity":integer(1,20),
    "active_count":integer(),"version":EXPECTED,"presence_expires_at":nullable(TIME)},
    ["id","display_name","presence","capacity","active_count","version","presence_expires_at"])
schema("StaffProfileInput",{"display_name":st(80,minLength=1),"capacity":integer(1,20),"expected_version":EXPECTED},
    ["display_name","capacity","expected_version"])
schema("StaffMessageInput",{"client_message_id":UUID,"text":st(10000),"attachment_ids":arr(UUID,5),"cards":arr(obj({
    "kind":en("PRODUCT","ORDER"),"id":JID},["kind","id"]),5)},["client_message_id","text","attachment_ids","cards"],
    "订单卡片必须属于当前会话会员，内容由服务端重读；员工不能自由粘贴他人订单对象。")
schema("Note",{"id":UUID,"admin_id":JID,"body":st(5000),"created_at":TIME,"retention_until":TIME},["id","admin_id","body","created_at","retention_until"])
schema("NoteInput",{"client_note_id":UUID,"body":st(5000,minLength=1)},["client_note_id","body"])
schema("FeedbackInput",{"vote":{"type":"integer","enum":[-1,1]},"reason":st(500)},["vote"])
S["NullableFeedback"] = nullable(ref("Feedback"))
schema("Feedback",{"id":UUID,"message_id":UUID,"vote":{"type":"integer","enum":[-1,1]},"reason":nullable(st(500))},
    ["id","message_id","vote","reason"])
schema("TranscriptionInput",{"attachment_id":UUID},["attachment_id"])
schema("Transcription",{"text":st(10000),"duration_ms":integer(1,60000),"editable":{"const":True}},["text","duration_ms","editable"])
schema("SpeechInput",{"voice":st(80),"part":integer(0,49)},[],"音色来自已授权TTS配置。服务端将本人可见回答按200字分段；part从0开始，不接受任意播报文本。")
schema("Speech",{"attachment":ref("Attachment"),"cache_hit":BOOL,"part_index":integer(0,49),"part_count":integer(1,50)},["attachment","cache_hit","part_index","part_count"])
schema("KBInput",{"title":st(200,minLength=1),"category":st(80,minLength=1),"visibility":en("MEMBER","STAFF"),
    "scope":obj({"product_ids":arr(JID,100),"category_ids":arr(JID,100)})},["title","category","visibility","scope"])
schema("KBUpdate",{"expected_version":EXPECTED,"title":st(200,minLength=1),"category":st(80,minLength=1),
    "visibility":en("MEMBER","STAFF"),"scope":S["KBInput"]["properties"]["scope"]},
    ["expected_version","title","category","visibility","scope"])
schema("KBDocument",{"id":UUID,"title":st(200),"category":st(80),"visibility":en("MEMBER","STAFF"),
    "scope":S["KBInput"]["properties"]["scope"],"enabled":BOOL,"active_version_id":nullable(UUID),"version":EXPECTED,"updated_at":TIME},
    ["id","title","category","visibility","scope","enabled","active_version_id","version","updated_at"])
schema("KBVersionInput",{"source_attachment_id":UUID,"source_text":st(500000),"change_note":st(500)},
    ["change_note"],"source_attachment_id 和 source_text 必须恰选一个；原文文件按 20MB、200 页建议限额校验。")
S["KBVersionInput"]["oneOf"]=[
    {"required":["source_attachment_id"],"not":{"required":["source_text"]}},
    {"required":["source_text"],"not":{"required":["source_attachment_id"]},"properties":{"source_text":{"minLength":1}}}
]
schema("KBVersion",{"id":UUID,"document_id":UUID,"version_no":integer(1),
    "state":en("DRAFT","PARSING","PREVIEW_READY","INDEXING","READY","FAILED","RETIRED"),"version":EXPECTED,
    "chunk_count":integer(),"embedding_model":nullable(st(160)),"embedding_dimension":nullable(integer(1)),
    "quality_passed":nullable(BOOL),"published_at":nullable(TIME),"created_at":TIME},
    ["id","document_id","version_no","state","version","chunk_count","embedding_model","embedding_dimension","quality_passed","published_at","created_at"])
schema("KBPreview",{"version_id":UUID,"parsed_text":st(500000),"warnings":arr(st(500),100),"page_count":nullable(integer(1))},
    ["version_id","parsed_text","warnings","page_count"])
schema("KBPreviewUpdate",{"expected_version":EXPECTED,"parsed_text":st(500000,minLength=1)},["expected_version","parsed_text"])
schema("KBPublishInput",{"expected_document_version":EXPECTED,"version_id":UUID},["expected_document_version","version_id"])
schema("KBEnabledInput",{"expected_version":EXPECTED,"enabled":BOOL},["expected_version","enabled"])
schema("SourceContent",{"source":ref("SourceCitation"),"excerpt":st(20000)},["source","excerpt"])
schema("RetentionResult",{"plan_id":UUID,"cutoff":TIME,"settings_version":EXPECTED,"settings_sha256":st(64),
    "messages":integer(),"conversations":integer(),"notes":integer(),"attachments":integer(),"protected_attachments":integer(),
    "execution_rechecks_protection":BOOL,"attachments_scheduled":integer(),"conversation_id":UUID,"context_expiry_cleanup":BOOL,"threads":integer()})
schema("Job",{"id":UUID,"job_type":st(32),"state":en("QUEUED","RUNNING","SUCCEEDED","FAILED","CANCELLED"),
    "progress":integer(0,100),"attempts":integer(),"version":EXPECTED,"error_code":nullable(st(80)),
    "error_message":nullable(st(500)),"summary":st(1000),"created_at":TIME,"updated_at":TIME,"result":nullable(ref("RetentionResult"))},
    ["id","job_type","state","progress","attempts","version","error_code","error_message","summary","created_at","updated_at"])
schema("ModelOptions",{"timeout_seconds":integer(1,180),"max_output_tokens":integer(1,16000),
    "retry_limit":integer(0,3),"daily_limit_units":integer(1,100000000),"dimension":integer(1,65536),"voice":st(80),"detail":en("auto")})
schema("ModelActivationInput",{"expected_version":EXPECTED,"compatibility_job_id":UUID},["expected_version","compatibility_job_id"])
schema("ModelRegistry",{"adapter_version":st(80),"entries":arr(obj({"capability":st(20),"provider":st(80),"endpoint_ref":st(160),"secret_ref":st(200),"secret_configured":BOOL},["capability","provider","endpoint_ref","secret_ref","secret_configured"]),6)},["adapter_version","entries"])
schema("ModelProfile",{"id":UUID,"name":st(100),"capability":en("DIALOG","EMBEDDING","RERANK","VISION","ASR","TTS"),
    "provider":st(80),"model_name":st(160),"endpoint_ref":st(160),"secret_configured":BOOL,
    "public_options":ref("ModelOptions"),"enabled":BOOL,"is_active":BOOL,"version":EXPECTED},
    ["id","name","capability","provider","model_name","endpoint_ref","secret_configured","public_options","enabled","is_active","version"])
schema("ModelProfileCreate",{"name":st(100,minLength=1),"capability":en("DIALOG","EMBEDDING","RERANK","VISION","ASR","TTS"),
    "provider":st(80),"model_name":st(160),"endpoint_ref":st(160),"secret_ref":st(200),"public_options":ref("ModelOptions")},
    ["name","capability","provider","model_name","endpoint_ref","secret_ref","public_options"],
    "secret_ref 为受控秘密配置引用，不接收原始 API key，也不在读取响应中返回。")
schema("ModelProfileUpdate",{"expected_version":EXPECTED,"name":st(100,minLength=1),"endpoint_ref":st(160),
    "secret_ref":st(200),"public_options":ref("ModelOptions"),"enabled":BOOL},
    ["expected_version","name","endpoint_ref","public_options","enabled"])
schema("PromptInput",{"prompt_key":st(80),"content":st(30000,minLength=1),"change_note":st(500)},
    ["prompt_key","content","change_note"])
schema("Prompt",{"id":UUID,"prompt_key":st(80),"version_no":integer(1),"state":en("DRAFT","PUBLISHED","RETIRED"),
    "content":st(30000),"version":EXPECTED,"created_at":TIME,"change_note":st(500)},["id","prompt_key","version_no","state","content","version","created_at","change_note"])
schema("QuotaLimit",{"scope":en("GLOBAL","CAPABILITY","PROFILE","MEMBER","STAFF"),"subject_id":nullable(st(80)),
    "capability":nullable(en("DIALOG","VISION","EMBEDDING","RERANK","ASR","TTS")),"unit":en("REQUEST","TOKEN","CHARACTER","SECOND"),
    "daily_limit":st(40,pattern=r"^[1-9][0-9]{0,13}(?:\.[0-9]{1,6})?$")},["scope","subject_id","capability","unit","daily_limit"])
schema("ModelPrice",{"profile_id":nullable(UUID),"capability":en("DIALOG","VISION","EMBEDDING","RERANK","ASR","TTS"),
    "unit":en("REQUEST","INPUT_TOKEN","OUTPUT_TOKEN","TOKEN","CHARACTER","SECOND","UNCACHED_INPUT_TOKEN","CACHE_READ_TOKEN","CACHE_WRITE_TOKEN"),"currency":en("CNY","USD"),
    "price":st(40,pattern=r"^(0|[1-9][0-9]{0,7})(?:\.[0-9]{1,8})?$"),"per_units":integer(1,1000000000),
    "model_name":nullable(st(160,minLength=1)),"source_ref":nullable(st(500,minLength=1))},
    ["profile_id","capability","unit","currency","price","per_units"])
schema("UsageSettlement",{"expected_version":EXPECTED,"input_tokens":nullable(integer(0,1000000000000)),"output_tokens":nullable(integer(0,1000000000000)),
    "total_tokens":nullable(integer(0,1000000000000)),"cache_read_tokens":nullable(integer(0,1000000000000)),"cache_write_tokens":nullable(integer(0,1000000000000)),"characters":nullable(integer(0,1000000000000)),"seconds":nullable(st(40,pattern=r"^(0|[1-9][0-9]{0,7})(?:\.[0-9]{1,6})?$")),
    "statement_ref":st(160,minLength=1),"reason":st(500,minLength=1)},["expected_version","statement_ref","reason"])
schema("UsageSettlementResult",{"call_id":UUID,"usage_state":{"const":"RECONCILED"},"version":EXPECTED,
    "estimated_cost":nullable(st(40)),"currency":nullable(en("CNY","USD"))},["call_id","usage_state","version","estimated_cost","currency"])
schema("UnknownModelUsage",{"call_id":UUID,"capability":nullable(st(20)),"provider_request_id":nullable(st(160)),"status":nullable(st(16)),
    "run_id":nullable(UUID),"job_id":nullable(UUID),"media_request_id":nullable(UUID),"actor_kind":en("MEMBER","STAFF","SYSTEM"),"actor_id":nullable(JID),"error_code":nullable(st(80)),
    "usage_state":st(16),"version":EXPECTED,"created_at":TIME,"reservations":arr(obj({"unit":st(24),"reserved_units":st(40)},["unit","reserved_units"]))},
    ["call_id","capability","provider_request_id","status","usage_state","version","created_at","reservations"])
schema("UnknownModelUsagePage",{"items":arr(ref("UnknownModelUsage")),"has_more":BOOL,"next_before":nullable(UUID)},["items","has_more","next_before"])
schema("ActionRecovery",{"action_id":UUID,"operation_no":UUID,"action_type":en(*CTYPES),"action_state":en("EXECUTING","UNKNOWN"),
    "action_version":EXPECTED,"dispatch_state":st(24),"dispatch_version":EXPECTED,"last_error_code":nullable(st(80)),"confirmed_at":nullable(TIME),"updated_at":TIME},
    ["action_id","operation_no","action_type","action_state","action_version","dispatch_state","dispatch_version","last_error_code","confirmed_at","updated_at"])
schema("ActionRecoveryPage",{"items":arr(ref("ActionRecovery")),"has_more":BOOL,"next_before":nullable(UUID)},["items","has_more","next_before"])
schema("ActionRecoveryInput",{"expected_version":EXPECTED,"expected_dispatch_version":EXPECTED,"reason":st(500,minLength=1)},["expected_version","expected_dispatch_version","reason"])
schema("ActionRecoveryResult",{"action_id":UUID,"action_state":st(24),"dispatch_state":{"const":"READY"},"dispatch_version":EXPECTED,"reconciliation_only":{"const":True}},
    ["action_id","action_state","dispatch_state","dispatch_version","reconciliation_only"])
schema("SettingsValue",{"message_retention_days":integer(1,3650),"image_retention_days":integer(1,3650),
    "audio_retention_days":integer(1,365),"tts_retention_days":integer(1,365),"event_retention_days":integer(1,90),"action_ttl_seconds":integer(60,900),
    "heartbeat_seconds":integer(10,60),"presence_timeout_seconds":integer(30,300),
    "max_input_characters":integer(1,10000),"max_document_pages":integer(1,2000),"quota_limits":arr(ref("QuotaLimit"),100),"model_prices":arr(ref("ModelPrice"),100)},
    ["message_retention_days","image_retention_days","audio_retention_days","tts_retention_days","event_retention_days","action_ttl_seconds","heartbeat_seconds",
     "presence_timeout_seconds","max_input_characters","max_document_pages"])
schema("Settings",{"value":ref("SettingsValue"),"version":EXPECTED},["value","version"])
schema("SettingsInput",{"value":ref("SettingsValue"),"expected_version":EXPECTED,"preview_job_id":UUID},
    ["value","expected_version"],"缩短留存期限必须携带同配置哈希的已成功试算任务，且试算不超过 24 小时。")
schema("RetentionPreviewInput",{"proposed_settings":ref("SettingsValue")},["proposed_settings"])
schema("RetentionExecuteInput",{"preview_job_id":UUID,"expected_settings_version":EXPECTED},
    ["preview_job_id","expected_settings_version"])
schema("Statistics",{"from":TIME,"to":TIME,"requests":integer(),"errors":integer(),"active_runs":integer(0,3),
    "queued_runs":integer(),"input_tokens":nullable(SEQ),"output_tokens":nullable(SEQ),"p95_business_ms":nullable(integer()),
    "p95_model_ms":nullable(integer()),"estimated_cost":nullable(st(40))},
    ["from","to","requests","errors","active_runs","queued_runs","input_tokens","output_tokens","p95_business_ms","p95_model_ms","estimated_cost"])
S["Statistics"]["properties"].update({"request_scope":{"const":"MODEL_CALLS"},"capabilities":arr(obj({"capability":st(20),"requests":integer(),"errors":integer(),"unknown_token_calls":integer(),"known_input_tokens":SEQ,"known_output_tokens":SEQ,"p95_ms":nullable(integer())})),
    "quota_usage":arr(obj({"unit_kind":st(24),"state":st(16),"reservations":integer(),"reserved_units":st(40),"settled_units":nullable(st(40))},["unit_kind","state","reservations","reserved_units","settled_units"])),
    "alerts":arr(obj({"code":st(80),"resource":st(120),"severity":en("WARNING","ERROR"),"count":integer()},["code","resource","severity","count"]))})
S["Statistics"]["properties"].update({"business":obj({"requests":integer(),"server_errors":integer(),"rejected_requests":integer(),"p95_ms":nullable(integer())},["requests","server_errors","rejected_requests","p95_ms"]),
    "cost_currency":nullable(en("CNY","USD")),"unpriced_calls":integer(),"costs":arr(obj({"currency":en("CNY","USD"),"calls":integer(),"estimated_cost":st(40)},["currency","calls","estimated_cost"]))})
S["Statistics"]["required"] += ["request_scope","capabilities","quota_usage","alerts","business","cost_currency","unpriced_calls","costs"]
schema("Audit",{"id":UUID,"actor_kind":st(16),"actor_id":nullable(JID),"operation":st(100),
    "resource_type":st(60),"resource_id":st(120),"request_id":UUID,"outcome":st(24),"summary":st(1000),"created_at":TIME},
    ["id","actor_kind","actor_id","operation","resource_type","resource_id","request_id","outcome","summary","created_at"])
schema("LogisticsInput",{"carrier":st(100),"tracking_no":st(100),"expected_version":EXPECTED},
    ["carrier","tracking_no","expected_version"],"首次创建时 expected_version=1，已有时使用读取版本；订单必须存在且允许员工访问。")
schema("LogisticsNodeInput",{"occurred_at":TIME,"location":st(200,minLength=1),"description":st(1000,minLength=1),
    "expected_shipment_version":EXPECTED},["occurred_at","location","description","expected_shipment_version"])
schema("LogisticsNodeUpdate",{"occurred_at":TIME,"location":st(200,minLength=1),"description":st(1000,minLength=1),
    "expected_version":EXPECTED,"reason":st(500,minLength=1)},["occurred_at","location","description","expected_version","reason"])
schema("ReturnReviewInput",{"expected_version":EXPECTED,"decision":en("APPROVE","REJECT"),"approved_amount":MONEY,
    "company_address_id":JID,"note":st(1000)},["expected_version","decision","note"],
    "APPROVE 必须 approved_amount 和有效 company_address_id，且金额不超过可申请金额；REJECT 不接收这两项。")
S["ReturnReviewInput"]["allOf"]=[
    {"if":{"properties":{"decision":{"const":"APPROVE"}}},"then":{"required":["approved_amount","company_address_id"]}},
    {"if":{"properties":{"decision":{"const":"REJECT"}}},"then":{"not":{"anyOf":[{"required":["approved_amount"]},{"required":["company_address_id"]}]}}}
]
schema("ReturnReceiveInput",{"expected_version":EXPECTED,"note":st(1000)},["expected_version","note"])
schema("ReturnRefundInput",{"expected_version":EXPECTED,"note":st(1000)},["expected_version","note"],
    "仅 GOODS_RECEIVED 可转 SIMULATED_REFUNDED，不调用真实支付或退款网关。")
schema("ReturnEligibility",{"order_id":JID,"order_item_id":JID,"eligible":BOOL,"remaining_quantity":integer(),
    "remaining_amount":MONEY,"deadline":nullable(TIME),"reason_codes":arr(obj({"code":st(80),"label":st(160)},["code","label"]),100),
    "failure_reason":nullable(st(500)),"rule_version":st(80)},
    ["order_id","order_item_id","eligible","remaining_quantity","remaining_amount","deadline","reason_codes","failure_reason","rule_version"])
schema("ProofAttestation",{"attachment_ref_id":UUID,"attachment_id":UUID,"sha256":st(64),"mime_type":st(100),
    "byte_size":integer(1,10485760),"attestation":st(4096)},["attachment_ref_id","attachment_id","sha256","mime_type","byte_size","attestation"],
    "受控附件服务签发，绑定会员/操作/内容哈希；Java 验证，不信任模型提供的链接。")
schema("ExecuteOperationInput",{"operation_no":UUID,"quote_id":UUID,"parameters_hash":st(64),
    "parameters":ref("ActionParameters"),"confirmation_receipt":st(4096),"proofs":arr(ref("ProofAttestation"),5)},
    ["operation_no","quote_id","parameters_hash","parameters","confirmation_receipt","proofs"],
    "confirmation_receipt 为受控确认服务签发，绑定会员、action、会话版本、参数哈希、有效期和实际点击记录。")
schema("MemberContext",{"member":ref("Identity"),"checkout_options":ref("CheckoutOptions")},["member","checkout_options"])
schema("StaffBusinessContext",{"member_display_name":st(80),"phone_masked":st(30),"orders":arr(ref("StaffOrderSummary"),20),"products":arr(ref("Product"),5)},
    ["member_display_name","phone_masked","orders","products"])
schema("BackupVerifyInput",{"backup_record_id":st(120),"note":st(500)},["backup_record_id","note"],
    "只接收受控备份目录登记 ID，不接受任意服务器路径、SQL、URL 或命令。")
schema("DependencySample",{"state":en("UP","DOWN","DISABLED"),"latency_ms":integer(0,300000),"error_code":nullable(st(80))},["state","latency_ms","error_code"])
schema("CloudRuntime",{k:integer() for k in ["memory_total_bytes","memory_available_bytes","disk_total_bytes","disk_free_bytes","swap_used_bytes","containers_running","containers_expected","containers_unhealthy","containers_oom","container_restarts"]},
    ["memory_total_bytes","memory_available_bytes","disk_total_bytes","disk_free_bytes","swap_used_bytes","containers_running","containers_expected","containers_unhealthy","containers_oom","container_restarts"])
schema("PostgresRuntime",{**{k:integer() for k in ["connections","max_connections","lock_waiters","longest_transaction_seconds","active_slots","queued_runs","oldest_queued_seconds","queued_jobs","oldest_job_seconds","job_wakeups_pending","event_notifications_pending","event_clock","event_replay_floor","event_clock_violations","pool_wait_p95_ms","pool_saturated_workers","first_text_samples"]},
    **{k:nullable(integer()) for k in ["first_text_p95_ms","queue_p95_ms","total_p95_ms"]}},
    ["connections","max_connections","lock_waiters","longest_transaction_seconds","active_slots","queued_runs","oldest_queued_seconds","queued_jobs","oldest_job_seconds","job_wakeups_pending","event_notifications_pending","event_clock","event_replay_floor","event_clock_violations","pool_wait_p95_ms","pool_saturated_workers","first_text_samples","first_text_p95_ms","queue_p95_ms","total_p95_ms"])
schema("JavaOutboxRuntime",{k:integer() for k in ["pending_due","overdue_60_seconds","oldest_due_seconds","retrying"]},["pending_due","overdue_60_seconds","oldest_due_seconds","retrying"])
schema("RuntimeSnapshot",{"checked_at":TIME,"duration_ms":integer(),"dependencies":{"type":"object","additionalProperties":ref("DependencySample")},
    "cloud":nullable(ref("CloudRuntime")),"postgres":nullable(ref("PostgresRuntime")),"java_outbox":nullable(ref("JavaOutboxRuntime"))},
    ["checked_at","duration_ms","dependencies","cloud","postgres","java_outbox"])
S["PostgresRuntime"]["properties"].update({"sse_cursor_expired_5m":integer(),"sse_invalid_cursor_5m":integer()})
S["PostgresRuntime"]["required"] += ["sse_cursor_expired_5m","sse_invalid_cursor_5m"]
S["Statistics"]["properties"]["runtime"]=nullable(ref("RuntimeSnapshot"))
S["BackupVerifyInput"]["properties"]["backup_record_id"]=UUID
schema("BackupPolicy",{"enabled":BOOL,"daily_hour_utc":integer(0,23),"restore_weekday_utc":integer(0,6),
    "retention_days":integer(7,90),"weekly_retention_weeks":integer(1,12),"journal_interval_minutes":integer(5,60),"note":st(500,minLength=1)},
    ["enabled","daily_hour_utc","restore_weekday_utc","retention_days","weekly_retention_weeks","journal_interval_minutes","note"])
schema("BackupPolicyState",{"value":ref("BackupPolicy"),"version":EXPECTED},["value","version"])
schema("BackupPolicyInput",{"value":ref("BackupPolicy"),"expected_version":EXPECTED},["value","expected_version"])
schema("BackupCreateInput",{"expected_policy_version":EXPECTED,"maintenance_confirmed":BOOL,"note":st(500,minLength=1)},["expected_policy_version","maintenance_confirmed","note"])
schema("BackupRecord",{"id":UUID,"state":en("AVAILABLE","EXPIRED"),"created_at":TIME,"artifacts":integer(),"object_count":integer(),
    "verified_at":nullable(TIME),"restore_seconds":nullable({"type":"number","minimum":0}),"last_restore_id":nullable(UUID)},
    ["id","state","created_at","artifacts","object_count","verified_at","restore_seconds","last_restore_id"])
schema("BackupExecutor",{"last_backup_at":nullable(TIME),"last_restore_at":nullable(TIME),"last_journal_at":nullable(TIME),"last_error":nullable(st(100)),"maintenance_stuck":BOOL,"updated_at":TIME},
    ["last_backup_at","last_restore_at","last_journal_at","last_error","maintenance_stuck","updated_at"])
schema("BackupPage",{"items":arr(ref("BackupRecord")),"has_more":BOOL,"next_before":nullable(UUID),"policy":ref("BackupPolicyState"),"executor":nullable(ref("BackupExecutor")),
    "alerts":arr(obj({"code":st(80),"resource":st(120),"severity":en("WARNING","ERROR"),"count":integer()},["code","resource","severity","count"]))},
    ["items","has_more","next_before","policy","executor","alerts"])
S["RetentionResult"]["properties"].update({"backup_record_id":UUID,"restore_id":UUID,"restore_job_id":UUID,"authenticated_artifacts":integer(),
    "restore_seconds":{"type":"number","minimum":0},"verified_at":TIME,"isolation_destroyed":BOOL,"mode":en("CREATE","VERIFY","RESTORE"),"verified":BOOL})

for n,item in [("ConversationPage",ref("Conversation")),("MessagePage",ref("Message")),("StaffPage",ref("Staff")),
    ("HandoffPage",ref("Handoff")),("NotePage",ref("Note")),("ProductPage",ref("Product")),("OrderPage",ref("OrderSummary")),
    ("ReturnPage",ref("ReturnSummary")),("KBDocumentPage",ref("KBDocument")),("KBVersionPage",ref("KBVersion")),
    ("JobPage",ref("Job")),("ModelProfilePage",ref("ModelProfile")),("PromptPage",ref("Prompt")),("AuditPage",ref("Audit"))]:
    page(n,item)

S["NotePage"]["properties"]["retention_cutoff"] = TIME

S["ProductPage"]["properties"]["filters"] = obj({
    "brands": arr(obj({"id":JID,"name":st(100)},["id","name"]),500),
    "categories": arr(obj({"id":JID,"name":st(100)},["id","name"]),500)
},["brands","categories"])
S["KBPreview"]["properties"]["pages"] = arr(obj({"page":integer(1,2000),"text":st(500000)},["page","text"]),2000)
S["KBPreviewUpdate"]["properties"]["pages"] = nullable(arr(obj({"page":integer(1,2000),"text":st(500000)},["page","text"]),2000))

def q(name,typ,desc="",required=False):
    return {"name":name,"in":"query","required":required,"schema":typ,"description":desc}
PAGING=[q("cursor",st(2048),"不透明稳定游标，不能由前端自行拼接"),q("limit",integer(1,100),"默认 20")]

def op(method,path,operation_id,title,result,reqs,ui,body=None,perm=None,params=(),status=200,tag=None,errors=(),multipart=False,stream=False,description=""):
    bridge="/agent-bridge/" in path
    staff="/admin/" in path or path.startswith("/mall-admin/")
    security={"StaffBearer" if staff else "MemberBearer":[]}
    if bridge: security["BridgeSignature"]=[]
    responses={}
    if stream:
        success={"description":"持久 SSE 事件，15 秒注释心跳；事件契约见 sse-events.schema.json",
                 "content":{"text/event-stream":{"schema":{"type":"string"}}},
                 "headers":{"Cache-Control":{"schema":{"const":"no-cache"}}}}
    elif result=="Binary":
        success={"description":"鉴权后的私有文件字节，不跳转至带令牌的 URL",
                 "content":{"application/octet-stream":{"schema":{"type":"string","format":"binary"}}},
                 "headers":{"Cache-Control":{"schema":{"const":"private, no-store"}}}}
    else:
        env=operation_id+"Response"
        schema(env,{"code":{"const":"OK"},"message":st(500),"data":ref(result),"request_id":UUID},
               ["code","message","data","request_id"])
        success={"description":"成功。异步受理返回 202，结果状态见 data。",
                 "content":{"application/json":{"schema":ref(env)}}}
    responses[str(status)]=success
    for status_code in [400,401,403,404,409,410,413,415,422,429,500,503]:
        responses[str(status_code)]={"$ref":"#/components/responses/StandardError"}
    parameters=list(params)
    for name in re.findall(r"{([^}]+)}",path):
        jt=name in {"product_id","order_id","order_item_id","return_id","staff_id","shipment_id","event_id"}
        parameters.append({"name":name,"in":"path","required":True,"schema":JID if jt else UUID})
    if method.lower() in {"post","put","patch","delete"}:
        parameters.append({"$ref":"#/components/parameters/IdempotencyKey"})
    parameters.append({"$ref":"#/components/parameters/RequestId"})
    if bridge:
        parameters.extend([{"$ref":"#/components/parameters/BridgeTimestamp"},{"$ref":"#/components/parameters/BridgeNonce"}])
    spec={"operationId":operation_id,"summary":title,"description":description or title,
          "tags":[tag or ("Java 员工桥接" if bridge and staff else "Java 会员桥接" if bridge else "员工端" if staff else "买家端")],
          "security":[security],"parameters":parameters,"responses":responses,"x-requirements":reqs,"x-ui-pages":ui,
          "x-business-errors":list(errors)}
    if perm: spec["x-required-permission"]=perm
    if body:
        spec["requestBody"]={"required":True,"content":{"multipart/form-data" if multipart else "application/json":{"schema":ref(body)}}}
    PATHS.setdefault(path,{})[method.lower()]=spec
    OPS.append({"id":operation_id,"method":method.upper(),"path":path,"title":title,"result":result,"body":body,
                "requirements":reqs,"ui":ui,"permission":perm,"status":status,"errors":list(errors)})

M="/mall-agent/v1/member"
A="/mall-agent/v1/admin"
J="/mall-portal/agent-bridge/v1"
K="/mall-admin/agent-bridge/v1"
op("get",M+"/me","memberMe","获取当前会员身份","Identity",["R01"],["B01"])
op("post",M+"/conversations","createConversation","创建本人会话","Conversation",["R02"],["B01","B02"],"ConversationCreate",status=201)
op("get",M+"/conversations","listConversations","分页查询本人会话","ConversationPage",["R02"],["B02"],params=PAGING+[q("query",st(100))])
op("get",M+"/conversations/{conversation_id}","getConversation","获取会话快照及事件高水位","Snapshot",["R02","R03"],["B02"])
op("patch",M+"/conversations/{conversation_id}","renameConversation","重命名本人会话","Conversation",["R02"],["B02"],"ConversationRename")
op("delete",M+"/conversations/{conversation_id}","deleteConversation","删除本人会话并使上下文失效","Empty",["R02","R17"],["B02"],"VersionInput")
op("get",M+"/conversations/{conversation_id}/messages","listMessages","查询本人可见消息","MessagePage",["R02","R03"],["B02"],params=PAGING+[q("before_seq",SEQ,"早于该消息序号，不能与 cursor 同用")])
op("post",M+"/conversations/{conversation_id}/messages","sendMemberMessage","幂等发送文字与附件","MessageAccepted",["R03","R13"],["B02"],"MessageInput",status=202,errors=["RUN_ALREADY_ACTIVE","MESSAGE_ID_REUSED"])
op("post",M+"/conversations/{conversation_id}/read","updateReadCursor","同步本人已读位置","ReadPosition",["R02"],["B02"],"ReadInput")
op("get",M+"/conversations/{conversation_id}/events","memberEvents","订阅及补传本人会话事件","Empty",["R03","R18"],["B02"],params=[q("after",SEQ,"重连游标，与 Last-Event-ID 同时提供时必须一致"),{"name":"Last-Event-ID","in":"header","required":False,"schema":SEQ}],stream=True)
op("get",M+"/runs/{run_id}","getRun","查询生成状态","Run",["R03"],["B02"])
op("post",M+"/runs/{run_id}/stop","stopRun","停止本人生成任务","Run",["R03"],["B02"],"VersionInput",status=202)
op("post",M+"/runs/{run_id}/retry","retryRun","重试回答且不重放业务操作","Run",["R03","R18"],["B02"],"VersionInput",status=202)
op("post",M+"/conversations/{conversation_id}/actions","prepareAction","准备不可变确认卡片","Action",["R08","R09","R11","R12","R18"],["B03","B04","B05"],"ActionPrepareInput",status=201)
op("get",M+"/actions/{action_id}","getAction","查询操作及业务核对结果","Action",["R18"],["B03","B04","B05"])
op("post",M+"/actions/{action_id}/confirm","confirmAction","用户明确点击确认执行","Action",["R08","R09","R11","R12","R18"],["B03","B04","B05"],"VersionInput",status=202,errors=["ACTION_EXPIRED","ACTION_STALE","QUOTE_CHANGED","STOCK_INSUFFICIENT","RESULT_UNKNOWN"],
   description="只接收 action 版本，不接收可变业务参数。服务端核验会员、会话当前版本及状态；重复同操作返回已有状态。模型工具和自然语言不能调用此确认入口。")
op("post",M+"/actions/{action_id}/cancel","cancelAction","取消尚未执行的卡片","Action",["R18"],["B03","B04","B05"],"VersionInput")
op("post",M+"/conversations/{conversation_id}/handoff","requestHandoff","申请转人工","Handoff",["R06"],["B02"],"HandoffInput",status=201)
op("delete",M+"/conversations/{conversation_id}/handoff","withdrawHandoff","撤销仍在等待的人工请求","Handoff",["R06"],["B02"],"VersionInput")
op("post",M+"/conversations/{conversation_id}/waiting-ai-mode","setWaitingAI","选择等待人工时是否继续 AI","Conversation",["R06"],["B02"],"WaitingAIInput")
schema("AttachmentUpload",{"purpose":en("CHAT_IMAGE","KNOWLEDGE","RECORDING"),"file":{"type":"string","format":"binary"}},["purpose","file"])
op("post",M+"/attachments","uploadMemberAttachment","上传本人图片或录音","Attachment",["R13","R14","R17"],["B02","B05"],"AttachmentUpload",multipart=True,status=201,
   description="会员仅允许 CHAT_IMAGE/RECORDING，KNOWLEDGE 必须 403；校验字节 MIME、大小、时长及额度，不信任扩展名。")
op("get",M+"/attachments/{attachment_id}/content","readMemberAttachment","鉴权读取本人私有附件","Binary",["R13","R14"],["B02","B05"])
op("post",M+"/audio/transcriptions","transcribeAudio","转写后返回可编辑文字","Transcription",["R14"],["B02"],"TranscriptionInput")
op("post",M+"/messages/{message_id}/speech","synthesizeSpeech","主动生成可见回答播报","Speech",["R14"],["B02"],"SpeechInput")
op("post",M+"/messages/{message_id}/feedback","submitFeedback","提交或替换本人回答反馈","Feedback",["R15"],["B02"],"FeedbackInput")
op("get",M+"/messages/{message_id}/feedback","readFeedback","读取本人回答反馈","NullableFeedback",["R15"],["B02"])
CATALOG_FILTERS = PAGING+[q("query",st(200)),q("min_price",MONEY),q("max_price",MONEY),q("category_id",JID),q("brand_id",JID),q("specification",st(100)),q("in_stock",BOOL),q("sort",en("newest","price_asc","price_desc","sales"))]
op("get",M+"/products","searchProducts","按预算规格检索当前可见商品","ProductPage",["R04"],["B01","B03"],params=CATALOG_FILTERS)
op("get",M+"/products/{product_id}","getProduct","读取商品实际规格与库存","Product",["R04","R08"],["B03"])
op("post",M+"/checkout-options","getCheckoutOptions","读取地址优惠与积分选项","CheckoutOptions",["R09"],["B04"],"CheckoutSelection")
op("get",M+"/orders","listMemberOrders","查询本人订单并支持订单号筛选","OrderPage",["R10"],["B04","B05"],params=PAGING+[q("order_sn",st(80)),q("status",integer(0,5))])
op("get",M+"/orders/{order_id}","getMemberOrder","读取本人订单详情","OrderSummary",["R10"],["B05"])
op("get",M+"/orders/{order_id}/logistics","getMemberLogistics","读取明确标记的模拟物流","Logistics",["R10"],["B05"])
op("get",M+"/orders/{order_id}/returns","listOrderReturns","读取本人订单售后摘要","ReturnPage",["R10","R12"],["B05"],params=PAGING)
op("get",M+"/orders/{order_id}/items/{order_item_id}/return-eligibility","checkReturnEligibility","核验具体明细的可退条件","ReturnEligibility",["R12"],["B05"])
op("get",M+"/citations/{version_id}/chunks/{chunk_id}","readCitation","读取仍可访问的知识来源片段","SourceContent",["R05"],["B02"],
   description="核验调用者、文档范围、版本与引用；停用或不可见返回 410 SOURCE_UNAVAILABLE，不泄漏原文。")

op("get",A+"/me","adminMe","获取员工稳定身份及权限","Identity",["R01"],["A01"])
op("get",A+"/conversations","adminListConversations","按分配关系查询接待会话","ConversationPage",["R06"],["A01"],perm="agent:conversation:read",params=PAGING+[q("bucket",en("WAITING","MINE","HISTORY")),q("query",st(100))])
op("get",A+"/conversations/{conversation_id}","adminConversationSnapshot","读取获授权会话快照","Snapshot",["R06"],["A01"],perm="agent:conversation:read")
op("get",A+"/conversations/{conversation_id}/messages","adminListMessages","读取可访问的买家消息","MessagePage",["R06"],["A01"],perm="agent:conversation:read",params=PAGING)
op("post",A+"/conversations/{conversation_id}/messages","sendStaffMessage","发送人工消息与业务卡片","Message",["R06"],["A01"],"StaffMessageInput","agent:conversation:reply",status=201)
op("get",A+"/conversations/{conversation_id}/context","getStaffBusinessContext","按授权查看关联会员和订单摘要","StaffBusinessContext",["R01","R06"],["A01"],perm="agent:business:read",params=[q("product_id",JID),q("order_id",JID)])
op("get",A+"/conversations/{conversation_id}/notes","listStaffNotes","读取独立内部备注","NotePage",["R06"],["A01"],perm="agent:conversation:note",params=PAGING)
op("post",A+"/conversations/{conversation_id}/notes","createStaffNote","添加不会进入模型的备注","Note",["R06"],["A01"],"NoteInput","agent:conversation:note",status=201)
op("post",A+"/conversations/{conversation_id}/read","adminReadCursor","同步员工已读位置","ReadPosition",["R06"],["A01"],"ReadInput","agent:conversation:read")
op("get",A+"/events","adminEvents","订阅已授权接待或管理事件","Empty",["R06","R07","R16"],["A01","A02","A04"],
   perm="agent:events:subscribe",params=[q("after",SEQ),q("conversation_id",UUID),{"name":"Last-Event-ID","in":"header","required":False,"schema":SEQ}],stream=True,
   description="按事件 required_permission 与当前分配关系逐条过滤；不能只凭订阅权限获取全量事件。权限变化立即影响补传与后续推送。")
op("get",A+"/handoffs","listHandoffs","读取等待接待队列","HandoffPage",["R06"],["A01"],perm="agent:handoff:claim",params=PAGING)
op("post",A+"/handoffs/{handoff_id}/claim","claimHandoff","原子领取并停止 AI","Handoff",["R06"],["A01"],"VersionInput","agent:handoff:claim",errors=["HANDOFF_ALREADY_CLAIMED","STAFF_CAPACITY_REACHED"])
op("post",A+"/handoffs/{handoff_id}/transfer","transferHandoff","按容量与权限转交接待","Handoff",["R06"],["A01"],"TransferInput","agent:handoff:transfer")
op("post",A+"/handoffs/{handoff_id}/close","closeHandoff","关闭接待和会话","Handoff",["R06"],["A01"],"CloseInput","agent:handoff:close")
op("get",A+"/staff","listStaff","查询可转交客服及容量","StaffPage",["R06"],["A01","A04"],perm="agent:staff:read",params=PAGING)
op("put",A+"/presence","setPresence","设置本人接待状态","Staff",["R06"],["A01"],"PresenceInput","agent:presence:write")
op("post",A+"/presence/heartbeat","presenceHeartbeat","续约本人在线状态","Staff",["R06"],["A01"],"Empty","agent:presence:write")
op("patch",A+"/staff/{staff_id}","updateStaffProfile","维护客服名称与容量","Staff",["R06","R16"],["A04"],"StaffProfileInput","agent:staff:manage")
op("post",A+"/attachments","uploadStaffAttachment","上传员工图片或知识原件","Attachment",["R07","R13"],["A01","A02"],"AttachmentUpload","agent:attachment:upload",multipart=True,status=201,
   description="CHAT_IMAGE 需要客服回复权限，KNOWLEDGE 还需知识编辑权限。逐用途校验，不能单凭上传权限扩大用途。")
op("get",A+"/attachments/{attachment_id}/content","readStaffAttachment","按会话知识或售后授权读取附件","Binary",["R01","R07","R13"],["A01","A02","A05"],perm="agent:attachment:read")
op("get",A+"/kb/documents","listKBDocuments","分页查询知识资料","KBDocumentPage",["R07"],["A02"],perm="agent:kb:read",params=PAGING+[q("query",st(200)),q("category",st(80))])
op("post",A+"/kb/documents","createKBDocument","创建知识逻辑文档","KBDocument",["R07"],["A02"],"KBInput","agent:kb:edit",status=201)
op("get",A+"/kb/documents/{document_id}","getKBDocument","读取知识资料元数据","KBDocument",["R07"],["A02"],perm="agent:kb:read")
op("patch",A+"/kb/documents/{document_id}","updateKBDocument","编辑知识标题范围和分类","KBDocument",["R07"],["A02"],"KBUpdate","agent:kb:edit")
op("get",A+"/kb/documents/{document_id}/versions","listKBVersions","查询版本历史","KBVersionPage",["R07"],["A02"],perm="agent:kb:read",params=PAGING)
op("post",A+"/kb/documents/{document_id}/versions","createKBVersion","创建待解析的资料版本","KBVersion",["R07"],["A02"],"KBVersionInput","agent:kb:edit",status=201)
op("get",A+"/kb/versions/{version_id}","getKBVersion","读取知识版本状态","KBVersion",["R07"],["A02"],perm="agent:kb:read")
op("post",A+"/kb/versions/{version_id}/parse","parseKBVersion","启动解析任务","Job",["R07"],["A02"],"VersionInput","agent:kb:edit",status=202)
op("get",A+"/kb/versions/{version_id}/preview","previewKBVersion","预览解析文本及警告","KBPreview",["R07"],["A02"],perm="agent:kb:read")
op("patch",A+"/kb/versions/{version_id}/preview","editKBPreview","编辑待构建版本解析结果","KBVersion",["R07"],["A02"],"KBPreviewUpdate","agent:kb:edit")
op("post",A+"/kb/versions/{version_id}/build","buildKBVersion","创建新索引并执行质量检查","Job",["R07"],["A02"],"VersionInput","agent:kb:edit",status=202)
op("post",A+"/kb/documents/{document_id}/publish","publishKBVersion","原子切换已就绪活动版本","KBDocument",["R07"],["A02"],"KBPublishInput","agent:kb:publish")
op("put",A+"/kb/documents/{document_id}/enabled","setKBEnabled","启用或立即停用知识来源","KBDocument",["R05","R07"],["A02"],"KBEnabledInput","agent:kb:publish")
op("get",A+"/jobs","listJobs","分页查询获授权任务","JobPage",["R07","R17","R18"],["A02","A04"],perm="agent:job:read",params=PAGING+[q("job_type",st(32)),q("state",st(32))])
op("get",A+"/jobs/{job_id}","getJob","查看进度和脱敏失败原因","Job",["R07","R17","R18"],["A02","A04"],perm="agent:job:read")
op("post",A+"/jobs/{job_id}/retry","retryJob","有限重试失败任务","Job",["R07","R17","R18"],["A02","A04"],"VersionInput","agent:job:retry",status=202,
   description="重试还必须拥有原任务的 kb:edit / retention:execute / backup:verify 等业务权限，不能以通用重试权限代替。")
op("get",A+"/model-profiles","listModelProfiles","读取脱敏模型能力配置","ModelProfilePage",["R16"],["A03"],perm="agent:model:read",params=PAGING)
op("get",A+"/model-registry","getModelRegistry","读取受控适配器与端点引用","ModelRegistry",["R16"],["A03"],perm="agent:model:read")
op("post",A+"/model-profiles/{profile_id}/test","testModelProfile","显式执行真实模型兼容验收","Job",["R16"],["A03"],"VersionInput","agent:model:write",status=202,
   description="显式付费验收，不由健康检查触发。工具调用、结构化输出或能力专用协议通过后，保存绑定当前配置哈希和适配器版本的任务结果。")
op("post",A+"/model-profiles","createModelProfile","新增独立能力配置引用","ModelProfile",["R16"],["A03"],"ModelProfileCreate","agent:model:write",status=201)
op("patch",A+"/model-profiles/{profile_id}","updateModelProfile","更新受控模型配置","ModelProfile",["R16"],["A03"],"ModelProfileUpdate","agent:model:write")
op("post",A+"/model-profiles/{profile_id}/activate","activateModelProfile","切换活动能力配置","ModelProfile",["R16"],["A03"],"ModelActivationInput","agent:model:activate",
   description="必须已有该配置版本的兼容验收记录；向量维度变化先重建并验证新索引。普通健康探针不发起付费模型调用。")
op("get",A+"/prompt-versions","listPrompts","查询获授权提示词版本","PromptPage",["R16"],["A03"],perm="agent:prompt:read",params=PAGING+[q("prompt_key",st(80))])
op("post",A+"/prompt-versions","createPrompt","创建提示词草稿版本","Prompt",["R16"],["A03"],"PromptInput","agent:prompt:write",status=201)
op("post",A+"/prompt-versions/{prompt_id}/publish","publishPrompt","发布或恢复指定提示词版本","Prompt",["R16"],["A03"],"VersionInput","agent:prompt:publish")
op("get",A+"/settings","getSettings","读取留存限额和接待参数","Settings",["R16","R17"],["A04"],perm="agent:settings:read")
op("put",A+"/settings","updateSettings","更新版本化设置","Settings",["R16","R17"],["A04"],"SettingsInput","agent:settings:write")
op("post",A+"/retention/preview","previewRetention","试算留存调整影响范围","Job",["R17"],["A04"],"RetentionPreviewInput","agent:retention:preview",status=202)
op("post",A+"/retention/execute","executeRetention","按已核对批次执行清理","Job",["R17"],["A04"],"RetentionExecuteInput","agent:retention:execute",status=202)
op("get",A+"/statistics","getStatistics","读取用量错误和延迟统计","Statistics",["R16"],["A03"],perm="agent:statistics:read",params=[q("from",TIME,required=True),q("to",TIME,required=True),q("capability",st(20))])
op("get",A+"/model-usage/unknown","listUnknownModelUsage","读取未知供应商用量及保守预留","UnknownModelUsagePage",["R16"],["A04"],perm="agent:usage:read",params=[q("before",UUID),q("limit",integer(1,100))])
op("post",A+"/model-usage/{call_id}/reconcile","reconcileModelUsage","按供应商凭据审计结算未知用量","UsageSettlementResult",["R16"],["A04"],"UsageSettlement","agent:usage:write")
op("get",A+"/actions/unresolved","listUnresolvedActions","读取长期未决业务操作","ActionRecoveryPage",["R18"],["A04"],perm="agent:action:read",params=[q("before",UUID),q("limit",integer(1,100))])
op("post",A+"/actions/{action_id}/reconcile","resumeActionReconciliation","续签原编号仅核对凭据并恢复调度","ActionRecoveryResult",["R18"],["A04"],"ActionRecoveryInput","agent:action:reconcile",status=202)
op("get",A+"/audit-logs","listAuditLogs","读取获授权审计摘要","AuditPage",["R16","R18"],["A04"],perm="agent:audit:read",params=PAGING+[q("request_id",UUID),q("resource_id",st(120))])
op("post",A+"/backups/verify","verifyBackup","对登记备份创建隔离恢复核对任务","Job",["R18"],["A04"],"BackupVerifyInput","agent:backup:verify",status=202)
op("get",A+"/backups","listBackups","读取登记备份策略与验证状态","BackupPage",["R18"],["A04"],perm="agent:backup:verify",params=[q("before",UUID),q("limit",integer(1,100))])
op("post",A+"/backups","createBackup","明确维护窗口后创建一致备份任务","Job",["R18"],["A04"],"BackupCreateInput","agent:backup:verify",status=202)
op("put",A+"/backups/policy","updateBackupPolicy","更新每日备份与每周恢复策略","BackupPolicyState",["R18"],["A04"],"BackupPolicyInput","agent:backup:verify",description="同时要求agent:settings:write；启用周期备份包含短维护窗口。")

op("get",J+"/identity","bridgeMemberIdentity","Java 复核会员最小身份","Identity",["R01"],["B01"])
op("get",J+"/products","bridgeProducts","Java 复核可见性预算和规格过滤","ProductPage",["R04"],["B03"],params=CATALOG_FILTERS)
op("get",J+"/products/{product_id}","bridgeProductDetail","Java 返回实际商品与 SKU","Product",["R04","R08"],["B03"])
op("post",J+"/checkout-options","bridgeCheckoutOptions","Java 返回可用地址优惠和积分","CheckoutOptions",["R09"],["B04"],"CheckoutSelection")
op("get",J+"/cart","bridgeCart","Java 分页读取当前会员购物车快照","CartSnapshot",["R08","R09"],["B04"],params=[q("limit",integer(1,20)),q("after",JID)])
op("post",J+"/quotes","bridgeQuote","Java 共用计算器报价且不写购物车","Quote",["R08","R09","R11","R12"],["B03","B04","B05"],"ActionParameters",status=201)
op("post",J+"/operations","executeBridgeOperation","Java 按操作编号事务执行一次","BusinessResult",["R08","R09","R11","R12","R18"],["B03","B04","B05"],"ExecuteOperationInput",
   description="同时验证会员令牌和服务签名；相同 operation_no 只有会员、类型及参数哈希都一致才能返回已有结果。报价过期或变化返回业务拒绝，不静默执行变更金额。")
op("get",J+"/operations/{operation_id}","getBridgeOperation","核对原操作已提交结果","BusinessResult",["R18"],["B03","B04","B05"])
op("get",J+"/orders","bridgeOrders","Java 查询本人订单号或近期订单","OrderPage",["R10"],["B05"],params=PAGING+[q("order_sn",st(80)),q("status",integer(0,5))])
op("get",J+"/orders/{order_id}","bridgeOrder","Java 读取本人订单详情","OrderSummary",["R10"],["B05"])
op("get",J+"/orders/{order_id}/logistics","bridgeLogistics","Java 读取本人模拟物流","Logistics",["R10"],["B05"])
op("get",J+"/orders/{order_id}/returns","bridgeReturns","Java 读取本人售后详情列表","ReturnPage",["R10","R12"],["B05"],params=PAGING)
op("get",J+"/orders/{order_id}/items/{order_item_id}/return-eligibility","bridgeReturnEligibility","Java 计算明细可退条件与金额","ReturnEligibility",["R12"],["B05"])
op("get",K+"/identity","bridgeStaffIdentity","Java 返回员工 ID 状态与权限","Identity",["R01"],["A01"])
schema("StaffContextInput",{"conversation_id":UUID,"access_grant":st(4096)},["conversation_id","access_grant"],
    "grant 由已校验会话分配的客服服务签发，绑定管理员、会员、会话、用途和短时有效期；Java 不接受任意 member_id。")
op("post",K+"/business-context","bridgeStaffContext","Java 核验员工代查业务上下文","StaffBusinessContext",["R01","R06"],["A01"],"StaffContextInput","agent:business:read")
op("put",K+"/orders/{order_id}/logistics","updateLogistics","维护订单模拟物流资料","Logistics",["R10"],["A05"],"LogisticsInput","agent:logistics:write")
op("post",K+"/shipments/{shipment_id}/events","createLogisticsNode","新增模拟物流节点","LogisticsNode",["R10"],["A05"],"LogisticsNodeInput","agent:logistics:write",status=201)
op("patch",K+"/shipments/{shipment_id}/events/{event_id}","updateLogisticsNode","编辑节点并保存前后审计","LogisticsNode",["R10"],["A05"],"LogisticsNodeUpdate","agent:logistics:write")
schema("StaffReturnPage",{"items":arr(ref("ReturnSummary"),100),"has_more":BOOL,"next_before":nullable(JID)},["items","has_more","next_before"])
schema("ReturnAddressPage",{"items":arr(ref("ReturnAddress"),100)},["items"])
op("get",K+"/returns","bridgeStaffReturns","查询演示售后列表","StaffReturnPage",["R12"],["A05"],perm="agent:return:read",params=[q("stage",st(32)),q("before",st(19)),q("limit",integer(1,100))])
op("get",K+"/return-addresses","bridgeReturnAddresses","读取可用退货地址","ReturnAddressPage",["R12"],["A05"],perm="agent:return:review")
op("get",A+"/returns","adminListReturns","查询有权处理的售后列表","StaffReturnPage",["R12"],["A05"],perm="agent:return:read",params=[q("stage",st(32)),q("before",st(19)),q("limit",integer(1,100))])
op("get",A+"/return-addresses","adminReturnAddresses","读取可用退货地址","ReturnAddressPage",["R12"],["A05"],perm="agent:return:review")
op("get",K+"/return-rules","bridgeListReturnRules","读取不可变售后规则版本","ReturnRulePage",["R12"],["A05"],perm="agent:return:read")
op("post",K+"/return-rules","bridgePublishReturnRule","发布新演示售后规则版本","ReturnRule",["R12"],["A05"],"ReturnRuleInput","agent:return:review")
op("get",A+"/return-rules","adminListReturnRules","读取售后规则及当前版本","ReturnRulePage",["R12"],["A05"],perm="agent:return:read")
op("post",A+"/return-rules","adminPublishReturnRule","发布不可变演示售后规则","ReturnRule",["R12"],["A05"],"ReturnRuleInput","agent:return:review")
op("post",K+"/returns/{return_id}/review","reviewReturn","人工批准或拒绝退货申请","ReturnSummary",["R12"],["A05"],"ReturnReviewInput","agent:return:review")
op("post",K+"/returns/{return_id}/receive","receiveReturn","人工登记收到退货","ReturnSummary",["R12"],["A05"],"ReturnReceiveInput","agent:return:receive")
op("post",K+"/returns/{return_id}/simulate-refund","simulateRefund","人工标记模拟退款完成","ReturnSummary",["R12"],["A05"],"ReturnRefundInput","agent:return:refund")
op("get",K+"/returns/{return_id}","getStaffReturn","读取有权处理的售后申请","ReturnSummary",["R12"],["A05"],perm="agent:return:read")
op("get",K+"/orders/{order_id}/logistics","getStaffLogistics","读取有权维护的物流资料","Logistics",["R10"],["A05"],perm="agent:logistics:read")

# 管理前端只访问此门面，服务端再签名调用 Java；浏览器永远不持有 BridgeSignature。
for method,suffix,oid,title,result,body,perm,reqs,status in [
 ("put","/orders/{order_id}/logistics","adminUpdateLogistics","维护模拟物流资料","Logistics","LogisticsInput","agent:logistics:write",["R10"],200),
 ("get","/orders/{order_id}/logistics","adminReadLogistics","读取模拟物流资料","Logistics",None,"agent:logistics:read",["R10"],200),
 ("post","/shipments/{shipment_id}/events","adminCreateLogisticsNode","新增模拟物流节点","LogisticsNode","LogisticsNodeInput","agent:logistics:write",["R10"],201),
 ("patch","/shipments/{shipment_id}/events/{event_id}","adminEditLogisticsNode","编辑模拟物流节点","LogisticsNode","LogisticsNodeUpdate","agent:logistics:write",["R10"],200),
 ("get","/returns/{return_id}","adminReadReturn","读取售后申请","ReturnSummary",None,"agent:return:read",["R12"],200),
 ("post","/returns/{return_id}/review","adminReviewReturn","人工审核退货申请","ReturnSummary","ReturnReviewInput","agent:return:review",["R12"],200),
 ("post","/returns/{return_id}/receive","adminReceiveReturn","登记收到退货","ReturnSummary","ReturnReceiveInput","agent:return:receive",["R12"],200),
 ("post","/returns/{return_id}/simulate-refund","adminSimulateRefund","标记模拟退款完成","ReturnSummary","ReturnRefundInput","agent:return:refund",["R12"],200)
]:
    op(method,A+suffix,oid,title,result,reqs,["A05"],body,perm,status=status,
       description=title+"。Python 门面使用当前员工令牌和服务签名调用 Java，由 Java 再次验证资源权限及状态；门面不直接写商城 MySQL。")

EVENTS={
 "message.accepted":obj({"message_id":UUID,"seq":SEQ,"client_message_id":nullable(UUID)},["message_id","seq","client_message_id"]),
 "run.started":obj({"run_id":UUID},["run_id"]),
 "run.queued":obj({"run_id":UUID,"position":integer(1)},["run_id","position"]),
 "message.delta":obj({"message_id":UUID,"run_id":UUID,"offset":integer(),"text":st(1000)},["message_id","run_id","offset","text"]),
 "message.completed":obj({"message":ref("Message")},["message"]),
 "action.updated":obj({"action":ref("Action")},["action"]),
 "handoff.updated":obj({"handoff":ref("Handoff"),"conversation":ref("Conversation")},["handoff","conversation"]),
 "run.failed":obj({"run_id":UUID,"error_code":st(80),"retryable":BOOL},["run_id","error_code","retryable"]),
 "run.stopped":obj({"run_id":UUID,"reason":en("USER","HANDOFF","SESSION_CHANGED")},["run_id","reason"]),
 "read.updated":ref("ReadPosition"),
 "conversation.updated":obj({"conversation":ref("Conversation")},["conversation"]),
 "job.updated":obj({"job":ref("Job")},["job"]),
 "note.created":obj({"note":ref("Note")},["note"])
}
event_variants=[]
for name,payload in EVENTS.items():
    event_variants.append(obj({"type":{"const":name},"event_id":SEQ,"conversation_id":nullable(UUID),
        "conversation_version":nullable(EXPECTED),"occurred_at":TIME,"payload":payload},
        ["type","event_id","conversation_id","conversation_version","occurred_at","payload"]))
S["StreamEvent"]={"oneOf":event_variants}

def generate():
    from architecture_contracts import extend_api
    extend_api(S, PATHS, OPS, op, ref, obj, arr, integer, st, UUID, TIME, EXPECTED, event_variants)
    out=ROOT/"api";out.mkdir(exist_ok=True)
    doc={"openapi":"3.1.0","jsonSchemaDialect":"https://json-schema.org/draft/2020-12/schema",
      "info":{"title":"商城客服与智能体接口设计","version":"0.2.0","description":"需求基线1.1的设计契约，所有新增接口尚未实现。会员、员工接口与 Java 受控桥接分别鉴权。"},
      "servers":[{"url":"/","description":"与现有商城同源网关；本地开发使用明确 CORS 源，不能带凭据使用星号源"}],
      "tags":[{"name":x} for x in ["买家端","员工端","Java 会员桥接","Java 员工桥接"]],
      "paths":PATHS,"components":{"schemas":S,
      "securitySchemes":{"OperationReceipt":{"type":"apiKey","in":"header","name":"X-Operation-Receipt","description":"仅核对凭据，绑定原操作/会员/类型/哈希，不授予业务执行权；仅服务端持有"},"MemberBearer":{"type":"http","scheme":"bearer","description":"Java 验证的会员 Sa-Token；不自行假定它是可离线校验 JWT"},
                        "StaffBearer":{"type":"http","scheme":"bearer","description":"Java 验证的员工 Sa-Token"},
                        "BridgeSignature":{"type":"apiKey","in":"header","name":"X-Agent-Signature","description":"key_id:base64url(HMAC-SHA256)，不含填充；规范签名串见架构03。普通桥接还需主体令牌，仅核对桥接还需OperationReceipt；密钥不交给模型或前端"}},
      "parameters":{"IdempotencyKey":{"name":"Idempotency-Key","in":"header","required":True,"schema":UUID,"description":"每次逻辑写请求固定 UUID；同主体/接口/键且不同内容返回409。消息另检查 client_message_id。"},
        "RequestId":{"name":"X-Request-ID","in":"header","required":False,"schema":UUID,"description":"服务端始终生成或验证并回传 request_id"},
        "BridgeTimestamp":{"name":"X-Agent-Timestamp","in":"header","required":True,"schema":{**TIME,"pattern":r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"},"description":"UTC RFC3339固定毫秒精度；按原字符串验签，允许偏差60秒"},
        "BridgeNonce":{"name":"X-Agent-Nonce","in":"header","required":True,"schema":UUID,"description":"5分钟内一次性验证；请求重试重新签名但保留原业务编号"}},
      "responses":{"StandardError":{"description":"按真实 HTTP 状态返回稳定错误码，data 为脱敏错误详情；SSE 建连后错误通过 run.failed 等事件表达。",
        "content":{"application/json":{"schema":ref("ErrorEnvelope")}},
        "headers":{"Retry-After":{"schema":{"type":"integer","minimum":1},"description":"仅429或可重试503返回秒数"}}}}}}
    (out/"openapi.json").write_text(json.dumps(doc,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    def external_refs(x):
        if isinstance(x,list): return [external_refs(a) for a in x]
        if isinstance(x,dict):
            return {k:(v.replace("#/components/schemas/","openapi.json#/components/schemas/") if k=="$ref" else external_refs(v)) for k,v in x.items()}
        return x
    ev={"$schema":"https://json-schema.org/draft/2020-12/schema","title":"SSE data JSON event","oneOf":external_refs(event_variants)}
    (out/"sse-events.schema.json").write_text(json.dumps(ev,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (out/"operation-catalog.json").write_text(json.dumps(OPS,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    return len(OPS)

if __name__=="__main__": print("API operations:",generate())
