// Generated from docs/api/openapi.json by mall-agent/scripts/generate_web_contracts.py.
// Wire types describe the full design; availability requires a real server capability.
// Java IDs and monetary values remain strings according to the API schema.

export type Action = { "id": string; "conversation_id": string; "action_type": "ADD_CART" | "CREATE_ORDER" | "CANCEL_ORDER" | "CREATE_RETURN"; "state": "PENDING_CONFIRMATION" | "EXECUTING" | "SUCCEEDED" | "FAILED" | "UNKNOWN" | "CANCELLED" | "EXPIRED" | "STALE"; "version": number; "conversation_version": number; "parameters": ActionParameters; "quote": Quote; "expires_at": string; "result": (BusinessResult | null); "error_code": (string | null); "control_epoch": number }
export type ActionParameters = (AddCartParameters | CreateOrderParameters | CancelOrderParameters | CreateReturnParameters)
export type ActionPrepareInput = { "expected_conversation_version": number; "parameters": ActionParameters }
export type ActionRecovery = { "action_id": string; "operation_no": string; "action_type": "ADD_CART" | "CREATE_ORDER" | "CANCEL_ORDER" | "CREATE_RETURN"; "action_state": "EXECUTING" | "UNKNOWN"; "action_version": number; "dispatch_state": string; "dispatch_version": number; "last_error_code": (string | null); "confirmed_at": (string | null); "updated_at": string }
export type ActionRecoveryInput = { "expected_version": number; "expected_dispatch_version": number; "reason": string }
export type ActionRecoveryPage = { "items": Array<ActionRecovery>; "has_more": boolean; "next_before": (string | null) }
export type ActionRecoveryResult = { "action_id": string; "action_state": string; "dispatch_state": "READY"; "dispatch_version": number; "reconciliation_only": true }
export type AddCartParameters = { "action_type": "ADD_CART"; "items": Array<SelectedItem> }
export type AddressOption = { "id": string; "label": string; "is_default": boolean }
export type Attachment = { "id": string; "name": string; "mime_type": string; "byte_size": number; "purpose": "CHAT_IMAGE" | "KNOWLEDGE" | "RECORDING" | "SPEECH"; "state": "UPLOADING" | "READY" | "FAILED" | "DELETING" | "DELETED"; "protected": boolean; "retention_until": (string | null) }
export type AttachmentUpload = { "purpose": "CHAT_IMAGE" | "KNOWLEDGE" | "RECORDING"; "file": string }
export type Audit = { "id": string; "actor_kind": string; "actor_id": (string | null); "operation": string; "resource_type": string; "resource_id": string; "request_id": string; "outcome": string; "summary": string; "created_at": string }
export type AuditPage = { "items": Array<Audit>; "next_cursor": (string | null); "has_more": boolean }
export type BackupCreateInput = { "expected_policy_version": number; "maintenance_confirmed": boolean; "note": string }
export type BackupExecutor = { "last_backup_at": (string | null); "last_restore_at": (string | null); "last_journal_at": (string | null); "last_error": (string | null); "maintenance_stuck": boolean; "updated_at": string }
export type BackupPage = { "items": Array<BackupRecord>; "has_more": boolean; "next_before": (string | null); "policy": BackupPolicyState; "executor": (BackupExecutor | null); "alerts": Array<{ "code": string; "resource": string; "severity": "WARNING" | "ERROR"; "count": number }> }
export type BackupPolicy = { "enabled": boolean; "daily_hour_utc": number; "restore_weekday_utc": number; "retention_days": number; "weekly_retention_weeks": number; "journal_interval_minutes": number; "note": string }
export type BackupPolicyInput = { "value": BackupPolicy; "expected_version": number }
export type BackupPolicyState = { "value": BackupPolicy; "version": number }
export type BackupRecord = { "id": string; "state": "AVAILABLE" | "EXPIRED"; "created_at": string; "artifacts": number; "object_count": number; "verified_at": (string | null); "restore_seconds": (number | null); "last_restore_id": (string | null) }
export type BackupVerifyInput = { "backup_record_id": string; "note": string }
export type BusinessResult = { "operation_no": string; "state": "SUCCEEDED" | "REJECTED" | "NOT_COMMITTED"; "business_type": ("CART" | "ORDER" | "ORDER_CANCEL" | "RETURN_APPLY" | null); "business_id": (string | null); "error_code": (string | null); "message": string; "order": (OrderSummary | null); "return_apply": (ReturnSummary | null); "cart_quantity": (number | null); "completed_at": (string | null) }
export type CancelOrderParameters = { "action_type": "CANCEL_ORDER"; "order_id": string }
export type CancellationSummary = { "id": string; "order_sn": string; "pay_amount": string; "status": 0 }
export type CartSnapshot = { "items": Array<CartSnapshotItem>; "next_cursor": (string | null); "has_more": boolean; "checked_at": string }
export type CartSnapshotItem = { "id": string; "product_id": (string | null); "sku_id": (string | null); "name": string; "specification": string; "quantity": number; "available": boolean; "available_quantity": number; "unit_price": (string | null) }
export type CheckoutOptions = { "addresses": Array<AddressOption>; "coupons": Array<CouponOption>; "available_points": number; "points_rule_description": string; "manage_address_path": string }
export type CheckoutSelection = ({ "action_type": "CREATE_ORDER"; "source": "CART" | "DIRECT"; "cart_item_ids"?: Array<string>; "items"?: Array<SelectedItem> } & { "action_type": "CREATE_ORDER"; "source": "CART" | "DIRECT"; "cart_item_ids"?: Array<string>; "items"?: Array<SelectedItem> })
export type CloseInput = { "expected_version": number; "reason": string }
export type CloudRuntime = { "memory_total_bytes": number; "memory_available_bytes": number; "disk_total_bytes": number; "disk_free_bytes": number; "swap_used_bytes": number; "containers_running": number; "containers_expected": number; "containers_unhealthy": number; "containers_oom": number; "container_restarts": number }
export type Conversation = { "id": string; "title": string; "state": "AI_ACTIVE" | "WAITING_HUMAN" | "HUMAN_ACTIVE" | "CLOSED"; "waiting_ai_enabled": boolean; "assigned_staff": ({ "id": string; "display_name": string; "presence": "ONLINE" | "BUSY" | "OFFLINE" } | null); "entry_context": EntryContext; "last_message_preview": string; "unread_count": number; "version": number; "created_at": string; "updated_at": string; "closed_at": (string | null); "control_epoch": number }
export type ConversationCreate = { "title"?: string; "entry_context": EntryContext }
export type ConversationPage = { "items": Array<Conversation>; "next_cursor": (string | null); "has_more": boolean; "event_cursor": string }
export type ConversationRename = { "title": string; "expected_version": number }
export type CouponOption = { "id": string; "name": string; "amount": string; "available": boolean; "unavailable_reason": (string | null) }
export type CreateOrderParameters = ({ "action_type": "CREATE_ORDER"; "source": "DIRECT" | "CART"; "items"?: Array<SelectedItem>; "cart_item_ids"?: Array<string>; "address_id": string; "coupon_id": (string | null); "use_points": number; "note"?: string } & { "action_type": "CREATE_ORDER"; "source": "DIRECT" | "CART"; "items"?: Array<SelectedItem>; "cart_item_ids"?: Array<string>; "address_id": string; "coupon_id": (string | null); "use_points": number; "note"?: string })
export type CreateReturnParameters = { "action_type": "CREATE_RETURN"; "order_id": string; "order_item_id": string; "quantity": number; "reason_code": string; "description": string; "attachment_ids": Array<string> }
export type DependencySample = { "state": "UP" | "DOWN" | "DISABLED"; "latency_ms": number; "error_code": (string | null) }
export type Empty = Record<string, unknown>
export type EntryContext = { "kind": "GENERAL" | "PRODUCT" | "ORDER"; "product_id"?: string; "order_id"?: string }
export type ErrorDetail = { "reason"?: string; "retry_after_seconds"?: number; "current_version"?: number; "resource_id"?: string; "field_errors"?: Array<{ "field": string; "message": string }> }
export type ErrorEnvelope = { "code": string; "message": string; "data": (ErrorDetail | null); "request_id": string }
export type ExecuteOperationInput = { "operation_no": string; "quote_id": string; "parameters_hash": string; "parameters": ActionParameters; "confirmation_receipt": string; "proofs": Array<ProofAttestation> }
export type Feedback = { "id": string; "message_id": string; "vote": -1 | 1; "reason": (string | null) }
export type FeedbackInput = { "vote": -1 | 1; "reason"?: string }
export type Handoff = { "id": string; "conversation_id": string; "state": "WAITING" | "CLAIMED" | "WITHDRAWN" | "CLOSED"; "version": number; "waiting_ai_enabled": boolean; "staff_id": (string | null); "created_at": string }
export type HandoffInput = { "expected_conversation_version": number; "reason"?: string }
export type HandoffPage = { "items": Array<Handoff>; "next_cursor": (string | null); "has_more": boolean; "event_cursor": string }
export type Identity = { "id": string; "kind": "MEMBER" | "STAFF"; "display_name": string; "permissions": Array<string>; "token_valid_until": (string | null) }
export type JavaOutboxRuntime = { "pending_due": number; "overdue_60_seconds": number; "oldest_due_seconds": number; "retrying": number }
export type Job = { "id": string; "job_type": string; "state": "QUEUED" | "RUNNING" | "SUCCEEDED" | "FAILED" | "CANCELLED"; "progress": number; "attempts": number; "version": number; "error_code": (string | null); "error_message": (string | null); "summary": string; "created_at": string; "updated_at": string; "result"?: (RetentionResult | null) }
export type JobPage = { "items": Array<Job>; "next_cursor": (string | null); "has_more": boolean; "event_cursor": string }
export type KBDocument = { "id": string; "title": string; "category": string; "visibility": "MEMBER" | "STAFF"; "scope": { "product_ids"?: Array<string>; "category_ids"?: Array<string> }; "enabled": boolean; "active_version_id": (string | null); "version": number; "updated_at": string }
export type KBDocumentPage = { "items": Array<KBDocument>; "next_cursor": (string | null); "has_more": boolean }
export type KBEnabledInput = { "expected_version": number; "enabled": boolean }
export type KBInput = { "title": string; "category": string; "visibility": "MEMBER" | "STAFF"; "scope": { "product_ids"?: Array<string>; "category_ids"?: Array<string> } }
export type KBPreview = { "version_id": string; "parsed_text": string; "warnings": Array<string>; "page_count": (number | null); "pages"?: Array<{ "page": number; "text": string }> }
export type KBPreviewUpdate = { "expected_version": number; "parsed_text": string; "pages"?: (Array<{ "page": number; "text": string }> | null) }
export type KBPublishInput = { "expected_document_version": number; "version_id": string }
export type KBUpdate = { "expected_version": number; "title": string; "category": string; "visibility": "MEMBER" | "STAFF"; "scope": { "product_ids"?: Array<string>; "category_ids"?: Array<string> } }
export type KBVersion = { "id": string; "document_id": string; "version_no": number; "state": "DRAFT" | "PARSING" | "PREVIEW_READY" | "INDEXING" | "READY" | "FAILED" | "RETIRED"; "version": number; "chunk_count": number; "embedding_model": (string | null); "embedding_dimension": (number | null); "quality_passed": (boolean | null); "published_at": (string | null); "created_at": string }
export type KBVersionInput = ({ "source_attachment_id": string; "source_text"?: string; "change_note": string } | { "source_attachment_id"?: string; "source_text": unknown; "change_note": string })
export type KBVersionPage = { "items": Array<KBVersion>; "next_cursor": (string | null); "has_more": boolean }
export type Logistics = { "order_id": string; "shipment_id": (string | null); "carrier": (string | null); "tracking_no": (string | null); "nodes": Array<LogisticsNode>; "version": number; "is_simulated": true }
export type LogisticsInput = { "carrier": string; "tracking_no": string; "expected_version": number }
export type LogisticsNode = { "id": string; "occurred_at": string; "location": string; "description": string; "version": number; "is_simulated": true }
export type LogisticsNodeInput = { "occurred_at": string; "location": string; "description": string; "expected_shipment_version": number }
export type LogisticsNodeUpdate = { "occurred_at": string; "location": string; "description": string; "expected_version": number; "reason": string }
export type MemberContext = { "member": Identity; "checkout_options": CheckoutOptions }
export type Message = { "id": string; "conversation_id": string; "seq": string; "sender_kind": "MEMBER" | "ASSISTANT" | "STAFF" | "SYSTEM"; "sender_display_name": string; "client_message_id": (string | null); "status": "ACCEPTED" | "GENERATING" | "COMPLETED" | "STOPPED" | "FAILED" | "INTERRUPTED"; "text": string; "blocks": Array<MessageBlock>; "run_id": (string | null); "reply_failure"?: (ReplyFailure | null); "created_at": string }
export type MessageAccepted = { "message": Message; "run": (Run | null); "event_cursor": string }
export type MessageBlock = ({ "type": "product"; "product": Product } | { "type": "order"; "order": OrderSummary } | { "type": "staff_order"; "staff_order": StaffOrderSummary } | { "type": "action"; "action": Action } | { "type": "source"; "source": SourceCitation } | { "type": "attachment"; "attachment": Attachment } | { "type": "logistics"; "logistics": Logistics })
export type MessageInput = { "client_message_id": string; "text": string; "attachment_ids": Array<string> }
export type MessagePage = { "items": Array<Message>; "next_cursor": (string | null); "has_more": boolean }
export type ModelActivationInput = { "expected_version": number; "compatibility_job_id": string }
export type ModelOptions = { "timeout_seconds"?: number; "max_output_tokens"?: number; "retry_limit"?: number; "daily_limit_units"?: number; "dimension"?: number; "voice"?: string; "detail"?: "auto" }
export type ModelPrice = { "profile_id": (string | null); "capability": "DIALOG" | "VISION" | "EMBEDDING" | "RERANK" | "ASR" | "TTS"; "unit": "REQUEST" | "INPUT_TOKEN" | "OUTPUT_TOKEN" | "TOKEN" | "CHARACTER" | "SECOND"; "currency": "CNY" | "USD"; "price": string; "per_units": number }
export type ModelProfile = { "id": string; "name": string; "capability": "DIALOG" | "EMBEDDING" | "RERANK" | "VISION" | "ASR" | "TTS"; "provider": string; "model_name": string; "endpoint_ref": string; "secret_configured": boolean; "public_options": ModelOptions; "enabled": boolean; "is_active": boolean; "version": number }
export type ModelProfileCreate = { "name": string; "capability": "DIALOG" | "EMBEDDING" | "RERANK" | "VISION" | "ASR" | "TTS"; "provider": string; "model_name": string; "endpoint_ref": string; "secret_ref": string; "public_options": ModelOptions }
export type ModelProfilePage = { "items": Array<ModelProfile>; "next_cursor": (string | null); "has_more": boolean }
export type ModelProfileUpdate = { "expected_version": number; "name": string; "endpoint_ref": string; "secret_ref"?: string; "public_options": ModelOptions; "enabled": boolean; "model_name": string }
export type ModelProfileVersion = { "profile_id": string; "version_no": number; "capability": string; "provider": string; "name": string; "enabled": boolean; "is_active": boolean; "model_name": string; "endpoint_ref": string; "public_options": ModelOptions; "snapshot_sha256": string; "compatibility_status": "UNVERIFIED" | "PASSED" | "FAILED"; "created_by": (string | null); "created_at": string }
export type ModelProfileVersionPage = { "items": Array<ModelProfileVersion>; "next_cursor": (string | null); "has_more": boolean }
export type ModelRegistry = { "adapter_version": string; "entries": Array<{ "capability": string; "provider": string; "endpoint_ref": string; "secret_ref": string; "secret_configured": boolean }> }
export type Note = { "id": string; "admin_id": string; "body": string; "created_at": string; "retention_until": string }
export type NoteInput = { "client_note_id": string; "body": string }
export type NotePage = { "items": Array<Note>; "next_cursor": (string | null); "has_more": boolean; "retention_cutoff"?: string }
export type NullableFeedback = (Feedback | null)
export type OrderLine = { "order_item_id": string; "product_id": string; "sku_id": string; "name": string; "specification": string; "quantity": number; "unit_price": string; "paid_line_amount": string; "returnable_quantity": number }
export type OrderPage = { "items": Array<OrderSummary>; "next_cursor": (string | null); "has_more": boolean }
export type OrderSummary = { "id": string; "order_sn": string; "status": number; "status_label": string; "pay_amount": string; "created_at": string; "receive_time": (string | null); "address_summary": string; "lines": Array<OrderLine>; "returns": Array<ReturnSummary>; "detail_path": string; "payment_path": (string | null); "is_simulated": boolean }
export type PostgresRuntime = { "connections": number; "max_connections": number; "lock_waiters": number; "longest_transaction_seconds": number; "active_slots": number; "queued_runs": number; "oldest_queued_seconds": number; "queued_jobs": number; "oldest_job_seconds": number; "job_wakeups_pending": number; "event_notifications_pending": number; "event_clock": number; "event_replay_floor": number; "event_clock_violations": number; "pool_wait_p95_ms": number; "pool_saturated_workers": number; "first_text_samples": number; "first_text_p95_ms": (number | null); "queue_p95_ms": (number | null); "total_p95_ms": (number | null); "sse_cursor_expired_5m": number; "sse_invalid_cursor_5m": number }
export type PresenceInput = { "presence": "ONLINE" | "BUSY" | "OFFLINE"; "expected_version": number }
export type Pricing = { "subtotal": string; "promotion_discount": string; "coupon_discount": string; "points_discount": string; "freight": string; "payable": string; "currency": "CNY" }
export type Product = { "id": string; "name": string; "brand": string; "category_id": string; "price_from": string; "image_url": string; "detail_path": string; "sku_options": Array<Sku>; "facts": Array<{ "name": string; "value": string }>; "checked_at": string }
export type ProductPage = { "items": Array<Product>; "next_cursor": (string | null); "has_more": boolean; "filters"?: { "brands": Array<{ "id": string; "name": string }>; "categories": Array<{ "id": string; "name": string }> } }
export type Prompt = { "id": string; "prompt_key": string; "version_no": number; "state": "DRAFT" | "PUBLISHED" | "RETIRED"; "content": string; "version": number; "created_at": string; "change_note": string }
export type PromptInput = { "prompt_key": string; "content": string; "change_note": string }
export type PromptPage = { "items": Array<Prompt>; "next_cursor": (string | null); "has_more": boolean }
export type ProofAttestation = { "attachment_ref_id": string; "attachment_id": string; "sha256": string; "mime_type": string; "byte_size": number; "attestation": string }
export type QuotaLimit = { "scope": "GLOBAL" | "CAPABILITY" | "PROFILE" | "MEMBER" | "STAFF"; "subject_id": (string | null); "capability": ("DIALOG" | "VISION" | "EMBEDDING" | "RERANK" | "ASR" | "TTS" | null); "unit": "REQUEST" | "TOKEN" | "CHARACTER" | "SECOND"; "daily_limit": string }
export type Quote = { "quote_id": string; "parameters": ActionParameters; "parameters_hash": string; "lines": Array<{ "product_id": string; "sku_id": string; "name": string; "specification": string; "quantity": number; "unit_price": string; "line_payable": string }>; "pricing": Pricing; "address_summary": (string | null); "rule_version": string; "expires_at": string; "cancellation"?: CancellationSummary }
export type ReadInput = { "last_seq": string }
export type ReadPosition = { "last_seq": string; "unread_count": number }
export type ReconcileOperationInput = { "mode": "QUERY_ONLY" | "FINALIZE_IF_ABSENT"; "parameters_hash": string }
export type ReplyFailure = { "code": string; "message": string; "retry_after": (string | null) }
export type RetentionExecuteInput = { "preview_job_id": string; "expected_settings_version": number }
export type RetentionPreviewInput = { "proposed_settings": SettingsValue }
export type RetentionResult = { "plan_id"?: string; "cutoff"?: string; "settings_version"?: number; "settings_sha256"?: string; "messages"?: number; "conversations"?: number; "notes"?: number; "attachments"?: number; "protected_attachments"?: number; "execution_rechecks_protection"?: boolean; "attachments_scheduled"?: number; "conversation_id"?: string; "context_expiry_cleanup"?: boolean; "threads"?: number; "backup_record_id"?: string; "restore_id"?: string; "restore_job_id"?: string; "authenticated_artifacts"?: number; "restore_seconds"?: number; "verified_at"?: string; "isolation_destroyed"?: boolean; "mode"?: "CREATE" | "VERIFY" | "RESTORE"; "verified"?: boolean }
export type ReturnAddress = { "id": string; "label": string }
export type ReturnAddressPage = { "items": Array<ReturnAddress> }
export type ReturnEligibility = { "order_id": string; "order_item_id": string; "eligible": boolean; "remaining_quantity": number; "remaining_amount": string; "deadline": (string | null); "reason_codes": Array<{ "code": string; "label": string }>; "failure_reason": (string | null); "rule_version": string }
export type ReturnPage = { "items": Array<ReturnSummary>; "next_cursor": (string | null); "has_more": boolean }
export type ReturnProof = { "attachment_id": string; "sha256": string; "mime_type": string; "byte_size": number }
export type ReturnReceiveInput = { "expected_version": number; "note": string }
export type ReturnRefundInput = { "expected_version": number; "note": string }
export type ReturnReviewInput = ({ "expected_version": number; "decision": "APPROVE" | "REJECT"; "approved_amount"?: string; "company_address_id"?: string; "note": string } & { "expected_version": number; "decision": "APPROVE" | "REJECT"; "approved_amount"?: string; "company_address_id"?: string; "note": string })
export type ReturnRule = { "id": string; "version_no": number; "rule_version": string; "return_window_hours": number; "allow_shipped": boolean; "amount_basis": "PAID_ITEM_NET"; "is_simulated": true; "effective_at": string; "note": string; "created_by": string; "created_at": string }
export type ReturnRuleInput = { "expected_latest_version": number; "return_window_hours": number; "allow_shipped": boolean; "effective_at": string; "note": string }
export type ReturnRulePage = { "items": Array<ReturnRule>; "active_rule_version": string }
export type ReturnSummary = { "id": string; "order_item_id": (string | null); "quantity": number; "requested_amount": string; "approved_amount": (string | null); "stage": "LEGACY" | "REQUESTED" | "APPROVED" | "GOODS_RECEIVED" | "SIMULATED_REFUNDED" | "REJECTED"; "legacy_status": (number | null); "reason": string; "version": number; "is_simulated": boolean; "created_at": string; "updated_at": string; "order_id"?: string; "description"?: string; "proofs"?: Array<ReturnProof>; "company_address"?: (ReturnAddress | null); "handle_note"?: (string | null); "receive_note"?: (string | null); "simulated_refund_no"?: (string | null); "rule_version"?: (string | null) }
export type Run = { "id": string; "conversation_id": string; "trigger_message_id": string; "state": "QUEUED" | "RUNNING" | "COMPLETED" | "STOP_REQUESTED" | "STOPPED" | "FAILED" | "INTERRUPTED"; "attempt": number; "queue_position": (number | null); "version": number; "error_code": (string | null); "created_at": string; "started_at": (string | null); "finished_at": (string | null); "control_epoch": number }
export type RuntimeSnapshot = { "checked_at": string; "duration_ms": number; "dependencies": Record<string, unknown> & Record<string, DependencySample>; "cloud": (CloudRuntime | null); "postgres": (PostgresRuntime | null); "java_outbox": (JavaOutboxRuntime | null) }
export type SelectedItem = { "product_id": string; "sku_id": string; "quantity": number }
export type Settings = { "value": SettingsValue; "version": number }
export type SettingsInput = { "value": SettingsValue; "expected_version": number; "preview_job_id"?: string }
export type SettingsValue = { "message_retention_days": number; "image_retention_days": number; "audio_retention_days": number; "tts_retention_days": number; "event_retention_days": number; "action_ttl_seconds": number; "heartbeat_seconds": number; "presence_timeout_seconds": number; "max_input_characters": number; "max_document_pages": number; "quota_limits"?: Array<QuotaLimit>; "model_prices"?: Array<ModelPrice> }
export type Sku = { "id": string; "attributes": Array<{ "name": string; "value": string }>; "price": string; "available_quantity": number; "visible": boolean }
export type Snapshot = { "conversation": Conversation; "latest_seq": string; "event_cursor": string; "active_run": (Run | null); "pending_actions": Array<Action>; "recent_messages": Array<Message>; "history_cursor": (string | null); "handoff": (Handoff | null); "read_position": ReadPosition }
export type SourceCitation = { "document_id": string; "version_id": string; "chunk_id": string; "title": string; "section": string; "page_start": (number | null); "page_end": (number | null); "available": boolean }
export type SourceContent = { "source": SourceCitation; "excerpt": string }
export type Speech = { "attachment": Attachment; "cache_hit": boolean; "part_index": number; "part_count": number }
export type SpeechInput = { "voice"?: string; "part"?: number }
export type Staff = { "id": string; "display_name": string; "presence": "ONLINE" | "BUSY" | "OFFLINE"; "capacity": number; "active_count": number; "version": number; "presence_expires_at": (string | null) }
export type StaffBusinessContext = { "member_display_name": string; "phone_masked": string; "orders": Array<StaffOrderSummary>; "products": Array<Product> }
export type StaffContextInput = { "conversation_id": string; "access_grant": string }
export type StaffMessageInput = { "client_message_id": string; "text": string; "attachment_ids": Array<string>; "cards": Array<{ "kind": "PRODUCT" | "ORDER"; "id": string }> }
export type StaffOrderSummary = { "id": string; "order_sn": string; "status": number; "status_label": string; "pay_amount": (string | null); "created_at": string; "lines": Array<{ "name": string; "specification": string; "quantity": number; "unit_price": (string | null) }> }
export type StaffPage = { "items": Array<Staff>; "next_cursor": (string | null); "has_more": boolean }
export type StaffProfileInput = { "display_name": string; "capacity": number; "expected_version": number }
export type StaffReturnPage = { "items": Array<ReturnSummary>; "has_more": boolean; "next_before": (string | null) }
export type Statistics = { "from": string; "to": string; "requests": number; "errors": number; "active_runs": number; "queued_runs": number; "input_tokens": (string | null); "output_tokens": (string | null); "p95_business_ms": (number | null); "p95_model_ms": (number | null); "estimated_cost": (string | null); "request_scope": "MODEL_CALLS"; "capabilities": Array<{ "capability"?: string; "requests"?: number; "errors"?: number; "unknown_token_calls"?: number; "known_input_tokens"?: string; "known_output_tokens"?: string; "p95_ms"?: (number | null) }>; "quota_usage": Array<{ "unit_kind": string; "state": string; "reservations": number; "reserved_units": string; "settled_units": (string | null) }>; "alerts": Array<{ "code": string; "resource": string; "severity": "WARNING" | "ERROR"; "count": number }>; "business": { "requests": number; "server_errors": number; "rejected_requests": number; "p95_ms": (number | null) }; "cost_currency": ("CNY" | "USD" | null); "unpriced_calls": number; "costs": Array<{ "currency": "CNY" | "USD"; "calls": number; "estimated_cost": string }>; "runtime"?: (RuntimeSnapshot | null) }
export type StreamEvent = ({ "type": "message.accepted"; "event_id": string; "conversation_id": (string | null); "conversation_version": (number | null); "occurred_at": string; "payload": { "message_id": string; "seq": string; "client_message_id": (string | null) }; "control_epoch": (number | null) } | { "type": "run.started"; "event_id": string; "conversation_id": (string | null); "conversation_version": (number | null); "occurred_at": string; "payload": { "run_id": string }; "control_epoch": (number | null) } | { "type": "run.queued"; "event_id": string; "conversation_id": (string | null); "conversation_version": (number | null); "occurred_at": string; "payload": { "run_id": string; "position": number }; "control_epoch": (number | null) } | { "type": "message.delta"; "event_id": string; "conversation_id": (string | null); "conversation_version": (number | null); "occurred_at": string; "payload": { "message_id": string; "run_id": string; "offset": number; "text": string }; "control_epoch": (number | null) } | { "type": "message.completed"; "event_id": string; "conversation_id": (string | null); "conversation_version": (number | null); "occurred_at": string; "payload": { "message": Message }; "control_epoch": (number | null) } | { "type": "action.updated"; "event_id": string; "conversation_id": (string | null); "conversation_version": (number | null); "occurred_at": string; "payload": { "action": Action }; "control_epoch": (number | null) } | { "type": "handoff.updated"; "event_id": string; "conversation_id": (string | null); "conversation_version": (number | null); "occurred_at": string; "payload": { "handoff": Handoff; "conversation": Conversation }; "control_epoch": (number | null) } | { "type": "run.failed"; "event_id": string; "conversation_id": (string | null); "conversation_version": (number | null); "occurred_at": string; "payload": { "run_id": string; "error_code": string; "retryable": boolean }; "control_epoch": (number | null) } | { "type": "run.stopped"; "event_id": string; "conversation_id": (string | null); "conversation_version": (number | null); "occurred_at": string; "payload": { "run_id": string; "reason": "USER" | "HANDOFF" | "SESSION_CHANGED" }; "control_epoch": (number | null) } | { "type": "read.updated"; "event_id": string; "conversation_id": (string | null); "conversation_version": (number | null); "occurred_at": string; "payload": ReadPosition; "control_epoch": (number | null) } | { "type": "conversation.updated"; "event_id": string; "conversation_id": (string | null); "conversation_version": (number | null); "occurred_at": string; "payload": { "conversation": Conversation }; "control_epoch": (number | null) } | { "type": "job.updated"; "event_id": string; "conversation_id": (string | null); "conversation_version": (number | null); "occurred_at": string; "payload": { "job": Job }; "control_epoch": (number | null) } | { "type": "note.created"; "event_id": string; "conversation_id": (string | null); "conversation_version": (number | null); "occurred_at": string; "payload": { "note": Note }; "control_epoch": (number | null) })
export type Transcription = { "text": string; "duration_ms": number; "editable": true }
export type TranscriptionInput = { "attachment_id": string }
export type TransferInput = { "expected_version": number; "target_staff_id": string; "reason": string }
export type UnknownModelUsage = { "call_id": string; "capability": (string | null); "provider_request_id": (string | null); "status": (string | null); "run_id"?: (string | null); "job_id"?: (string | null); "media_request_id"?: (string | null); "actor_kind"?: "MEMBER" | "STAFF" | "SYSTEM"; "actor_id"?: (string | null); "error_code"?: (string | null); "usage_state": string; "version": number; "created_at": string; "reservations": Array<{ "unit": string; "reserved_units": string }> }
export type UnknownModelUsagePage = { "items": Array<UnknownModelUsage>; "has_more": boolean; "next_before": (string | null) }
export type UsageSettlement = { "expected_version": number; "input_tokens"?: (number | null); "output_tokens"?: (number | null); "total_tokens"?: (number | null); "characters"?: (number | null); "seconds"?: (string | null); "statement_ref": string; "reason": string }
export type UsageSettlementResult = { "call_id": string; "usage_state": "RECONCILED"; "version": number; "estimated_cost": (string | null); "currency": ("CNY" | "USD" | null) }
export type VersionInput = { "expected_version": number }
export type WaitingAIInput = { "expected_version": number; "enabled": boolean }
export type activateModelProfileResponse = { "code": "OK"; "message": string; "data": ModelProfile; "request_id": string }
export type adminConversationSnapshotResponse = { "code": "OK"; "message": string; "data": Snapshot; "request_id": string }
export type adminCreateLogisticsNodeResponse = { "code": "OK"; "message": string; "data": LogisticsNode; "request_id": string }
export type adminEditLogisticsNodeResponse = { "code": "OK"; "message": string; "data": LogisticsNode; "request_id": string }
export type adminListConversationsResponse = { "code": "OK"; "message": string; "data": ConversationPage; "request_id": string }
export type adminListMessagesResponse = { "code": "OK"; "message": string; "data": MessagePage; "request_id": string }
export type adminListReturnRulesResponse = { "code": "OK"; "message": string; "data": ReturnRulePage; "request_id": string }
export type adminListReturnsResponse = { "code": "OK"; "message": string; "data": StaffReturnPage; "request_id": string }
export type adminMeResponse = { "code": "OK"; "message": string; "data": Identity; "request_id": string }
export type adminPublishReturnRuleResponse = { "code": "OK"; "message": string; "data": ReturnRule; "request_id": string }
export type adminReadCursorResponse = { "code": "OK"; "message": string; "data": ReadPosition; "request_id": string }
export type adminReadLogisticsResponse = { "code": "OK"; "message": string; "data": Logistics; "request_id": string }
export type adminReadReturnResponse = { "code": "OK"; "message": string; "data": ReturnSummary; "request_id": string }
export type adminReceiveReturnResponse = { "code": "OK"; "message": string; "data": ReturnSummary; "request_id": string }
export type adminReturnAddressesResponse = { "code": "OK"; "message": string; "data": ReturnAddressPage; "request_id": string }
export type adminReviewReturnResponse = { "code": "OK"; "message": string; "data": ReturnSummary; "request_id": string }
export type adminSimulateRefundResponse = { "code": "OK"; "message": string; "data": ReturnSummary; "request_id": string }
export type adminUpdateLogisticsResponse = { "code": "OK"; "message": string; "data": Logistics; "request_id": string }
export type bridgeCartResponse = { "code": "OK"; "message": string; "data": CartSnapshot; "request_id": string }
export type bridgeCheckoutOptionsResponse = { "code": "OK"; "message": string; "data": CheckoutOptions; "request_id": string }
export type bridgeListReturnRulesResponse = { "code": "OK"; "message": string; "data": ReturnRulePage; "request_id": string }
export type bridgeLogisticsResponse = { "code": "OK"; "message": string; "data": Logistics; "request_id": string }
export type bridgeMemberIdentityResponse = { "code": "OK"; "message": string; "data": Identity; "request_id": string }
export type bridgeOrderResponse = { "code": "OK"; "message": string; "data": OrderSummary; "request_id": string }
export type bridgeOrdersResponse = { "code": "OK"; "message": string; "data": OrderPage; "request_id": string }
export type bridgeProductDetailResponse = { "code": "OK"; "message": string; "data": Product; "request_id": string }
export type bridgeProductsResponse = { "code": "OK"; "message": string; "data": ProductPage; "request_id": string }
export type bridgePublishReturnRuleResponse = { "code": "OK"; "message": string; "data": ReturnRule; "request_id": string }
export type bridgeQuoteResponse = { "code": "OK"; "message": string; "data": Quote; "request_id": string }
export type bridgeReturnAddressesResponse = { "code": "OK"; "message": string; "data": ReturnAddressPage; "request_id": string }
export type bridgeReturnEligibilityResponse = { "code": "OK"; "message": string; "data": ReturnEligibility; "request_id": string }
export type bridgeReturnsResponse = { "code": "OK"; "message": string; "data": ReturnPage; "request_id": string }
export type bridgeStaffContextResponse = { "code": "OK"; "message": string; "data": StaffBusinessContext; "request_id": string }
export type bridgeStaffIdentityResponse = { "code": "OK"; "message": string; "data": Identity; "request_id": string }
export type bridgeStaffReturnsResponse = { "code": "OK"; "message": string; "data": StaffReturnPage; "request_id": string }
export type buildKBVersionResponse = { "code": "OK"; "message": string; "data": Job; "request_id": string }
export type cancelActionResponse = { "code": "OK"; "message": string; "data": Action; "request_id": string }
export type checkReturnEligibilityResponse = { "code": "OK"; "message": string; "data": ReturnEligibility; "request_id": string }
export type claimHandoffResponse = { "code": "OK"; "message": string; "data": Handoff; "request_id": string }
export type closeHandoffResponse = { "code": "OK"; "message": string; "data": Handoff; "request_id": string }
export type confirmActionResponse = { "code": "OK"; "message": string; "data": Action; "request_id": string }
export type createBackupResponse = { "code": "OK"; "message": string; "data": Job; "request_id": string }
export type createConversationResponse = { "code": "OK"; "message": string; "data": Conversation; "request_id": string }
export type createKBDocumentResponse = { "code": "OK"; "message": string; "data": KBDocument; "request_id": string }
export type createKBVersionResponse = { "code": "OK"; "message": string; "data": KBVersion; "request_id": string }
export type createLogisticsNodeResponse = { "code": "OK"; "message": string; "data": LogisticsNode; "request_id": string }
export type createModelProfileResponse = { "code": "OK"; "message": string; "data": ModelProfile; "request_id": string }
export type createPromptResponse = { "code": "OK"; "message": string; "data": Prompt; "request_id": string }
export type createStaffNoteResponse = { "code": "OK"; "message": string; "data": Note; "request_id": string }
export type deleteConversationResponse = { "code": "OK"; "message": string; "data": Empty; "request_id": string }
export type editKBPreviewResponse = { "code": "OK"; "message": string; "data": KBVersion; "request_id": string }
export type executeBridgeOperationResponse = { "code": "OK"; "message": string; "data": BusinessResult; "request_id": string }
export type executeRetentionResponse = { "code": "OK"; "message": string; "data": Job; "request_id": string }
export type getActionResponse = { "code": "OK"; "message": string; "data": Action; "request_id": string }
export type getBridgeOperationResponse = { "code": "OK"; "message": string; "data": BusinessResult; "request_id": string }
export type getCheckoutOptionsResponse = { "code": "OK"; "message": string; "data": CheckoutOptions; "request_id": string }
export type getConversationResponse = { "code": "OK"; "message": string; "data": Snapshot; "request_id": string }
export type getJobResponse = { "code": "OK"; "message": string; "data": Job; "request_id": string }
export type getKBDocumentResponse = { "code": "OK"; "message": string; "data": KBDocument; "request_id": string }
export type getKBVersionResponse = { "code": "OK"; "message": string; "data": KBVersion; "request_id": string }
export type getMemberLogisticsResponse = { "code": "OK"; "message": string; "data": Logistics; "request_id": string }
export type getMemberOrderResponse = { "code": "OK"; "message": string; "data": OrderSummary; "request_id": string }
export type getModelRegistryResponse = { "code": "OK"; "message": string; "data": ModelRegistry; "request_id": string }
export type getProductResponse = { "code": "OK"; "message": string; "data": Product; "request_id": string }
export type getRunResponse = { "code": "OK"; "message": string; "data": Run; "request_id": string }
export type getSettingsResponse = { "code": "OK"; "message": string; "data": Settings; "request_id": string }
export type getStaffBusinessContextResponse = { "code": "OK"; "message": string; "data": StaffBusinessContext; "request_id": string }
export type getStaffLogisticsResponse = { "code": "OK"; "message": string; "data": Logistics; "request_id": string }
export type getStaffReturnResponse = { "code": "OK"; "message": string; "data": ReturnSummary; "request_id": string }
export type getStatisticsResponse = { "code": "OK"; "message": string; "data": Statistics; "request_id": string }
export type listAuditLogsResponse = { "code": "OK"; "message": string; "data": AuditPage; "request_id": string }
export type listBackupsResponse = { "code": "OK"; "message": string; "data": BackupPage; "request_id": string }
export type listConversationsResponse = { "code": "OK"; "message": string; "data": ConversationPage; "request_id": string }
export type listHandoffsResponse = { "code": "OK"; "message": string; "data": HandoffPage; "request_id": string }
export type listJobsResponse = { "code": "OK"; "message": string; "data": JobPage; "request_id": string }
export type listKBDocumentsResponse = { "code": "OK"; "message": string; "data": KBDocumentPage; "request_id": string }
export type listKBVersionsResponse = { "code": "OK"; "message": string; "data": KBVersionPage; "request_id": string }
export type listMemberOrdersResponse = { "code": "OK"; "message": string; "data": OrderPage; "request_id": string }
export type listMessagesResponse = { "code": "OK"; "message": string; "data": MessagePage; "request_id": string }
export type listModelProfileVersionsResponse = { "code": "OK"; "message": string; "data": ModelProfileVersionPage; "request_id": string }
export type listModelProfilesResponse = { "code": "OK"; "message": string; "data": ModelProfilePage; "request_id": string }
export type listOrderReturnsResponse = { "code": "OK"; "message": string; "data": ReturnPage; "request_id": string }
export type listPromptsResponse = { "code": "OK"; "message": string; "data": PromptPage; "request_id": string }
export type listStaffNotesResponse = { "code": "OK"; "message": string; "data": NotePage; "request_id": string }
export type listStaffResponse = { "code": "OK"; "message": string; "data": StaffPage; "request_id": string }
export type listUnknownModelUsageResponse = { "code": "OK"; "message": string; "data": UnknownModelUsagePage; "request_id": string }
export type listUnresolvedActionsResponse = { "code": "OK"; "message": string; "data": ActionRecoveryPage; "request_id": string }
export type memberMeResponse = { "code": "OK"; "message": string; "data": Identity; "request_id": string }
export type parseKBVersionResponse = { "code": "OK"; "message": string; "data": Job; "request_id": string }
export type prepareActionResponse = { "code": "OK"; "message": string; "data": Action; "request_id": string }
export type presenceHeartbeatResponse = { "code": "OK"; "message": string; "data": Staff; "request_id": string }
export type previewKBVersionResponse = { "code": "OK"; "message": string; "data": KBPreview; "request_id": string }
export type previewRetentionResponse = { "code": "OK"; "message": string; "data": Job; "request_id": string }
export type publishKBVersionResponse = { "code": "OK"; "message": string; "data": KBDocument; "request_id": string }
export type publishPromptResponse = { "code": "OK"; "message": string; "data": Prompt; "request_id": string }
export type readCitationResponse = { "code": "OK"; "message": string; "data": SourceContent; "request_id": string }
export type readFeedbackResponse = { "code": "OK"; "message": string; "data": NullableFeedback; "request_id": string }
export type receiveReturnResponse = { "code": "OK"; "message": string; "data": ReturnSummary; "request_id": string }
export type reconcileBridgeOperationResponse = { "code": "OK"; "message": string; "data": BusinessResult; "request_id": string }
export type reconcileModelUsageResponse = { "code": "OK"; "message": string; "data": UsageSettlementResult; "request_id": string }
export type renameConversationResponse = { "code": "OK"; "message": string; "data": Conversation; "request_id": string }
export type requestHandoffResponse = { "code": "OK"; "message": string; "data": Handoff; "request_id": string }
export type resumeActionReconciliationResponse = { "code": "OK"; "message": string; "data": ActionRecoveryResult; "request_id": string }
export type retryJobResponse = { "code": "OK"; "message": string; "data": Job; "request_id": string }
export type retryRunResponse = { "code": "OK"; "message": string; "data": Run; "request_id": string }
export type reviewReturnResponse = { "code": "OK"; "message": string; "data": ReturnSummary; "request_id": string }
export type searchProductsResponse = { "code": "OK"; "message": string; "data": ProductPage; "request_id": string }
export type sendMemberMessageResponse = { "code": "OK"; "message": string; "data": MessageAccepted; "request_id": string }
export type sendStaffMessageResponse = { "code": "OK"; "message": string; "data": Message; "request_id": string }
export type setKBEnabledResponse = { "code": "OK"; "message": string; "data": KBDocument; "request_id": string }
export type setPresenceResponse = { "code": "OK"; "message": string; "data": Staff; "request_id": string }
export type setWaitingAIResponse = { "code": "OK"; "message": string; "data": Conversation; "request_id": string }
export type simulateRefundResponse = { "code": "OK"; "message": string; "data": ReturnSummary; "request_id": string }
export type stopRunResponse = { "code": "OK"; "message": string; "data": Run; "request_id": string }
export type submitFeedbackResponse = { "code": "OK"; "message": string; "data": Feedback; "request_id": string }
export type synthesizeSpeechResponse = { "code": "OK"; "message": string; "data": Speech; "request_id": string }
export type testModelProfileResponse = { "code": "OK"; "message": string; "data": Job; "request_id": string }
export type transcribeAudioResponse = { "code": "OK"; "message": string; "data": Transcription; "request_id": string }
export type transferHandoffResponse = { "code": "OK"; "message": string; "data": Handoff; "request_id": string }
export type updateBackupPolicyResponse = { "code": "OK"; "message": string; "data": BackupPolicyState; "request_id": string }
export type updateKBDocumentResponse = { "code": "OK"; "message": string; "data": KBDocument; "request_id": string }
export type updateLogisticsNodeResponse = { "code": "OK"; "message": string; "data": LogisticsNode; "request_id": string }
export type updateLogisticsResponse = { "code": "OK"; "message": string; "data": Logistics; "request_id": string }
export type updateModelProfileResponse = { "code": "OK"; "message": string; "data": ModelProfile; "request_id": string }
export type updateReadCursorResponse = { "code": "OK"; "message": string; "data": ReadPosition; "request_id": string }
export type updateSettingsResponse = { "code": "OK"; "message": string; "data": Settings; "request_id": string }
export type updateStaffProfileResponse = { "code": "OK"; "message": string; "data": Staff; "request_id": string }
export type uploadMemberAttachmentResponse = { "code": "OK"; "message": string; "data": Attachment; "request_id": string }
export type uploadStaffAttachmentResponse = { "code": "OK"; "message": string; "data": Attachment; "request_id": string }
export type verifyBackupResponse = { "code": "OK"; "message": string; "data": Job; "request_id": string }
export type withdrawHandoffResponse = { "code": "OK"; "message": string; "data": Handoff; "request_id": string }

export type AgentOperations = {
  "memberMe": { method: "GET"; path: "/mall-agent/v1/member/me"; request: unknown; response: memberMeResponse }
  "createConversation": { method: "POST"; path: "/mall-agent/v1/member/conversations"; request: ConversationCreate; response: createConversationResponse }
  "listConversations": { method: "GET"; path: "/mall-agent/v1/member/conversations"; request: unknown; response: listConversationsResponse }
  "getConversation": { method: "GET"; path: "/mall-agent/v1/member/conversations/{conversation_id}"; request: unknown; response: getConversationResponse }
  "renameConversation": { method: "PATCH"; path: "/mall-agent/v1/member/conversations/{conversation_id}"; request: ConversationRename; response: renameConversationResponse }
  "deleteConversation": { method: "DELETE"; path: "/mall-agent/v1/member/conversations/{conversation_id}"; request: VersionInput; response: deleteConversationResponse }
  "listMessages": { method: "GET"; path: "/mall-agent/v1/member/conversations/{conversation_id}/messages"; request: unknown; response: listMessagesResponse }
  "sendMemberMessage": { method: "POST"; path: "/mall-agent/v1/member/conversations/{conversation_id}/messages"; request: MessageInput; response: sendMemberMessageResponse }
  "updateReadCursor": { method: "POST"; path: "/mall-agent/v1/member/conversations/{conversation_id}/read"; request: ReadInput; response: updateReadCursorResponse }
  "memberEvents": { method: "GET"; path: "/mall-agent/v1/member/conversations/{conversation_id}/events"; request: unknown; response: unknown }
  "getRun": { method: "GET"; path: "/mall-agent/v1/member/runs/{run_id}"; request: unknown; response: getRunResponse }
  "stopRun": { method: "POST"; path: "/mall-agent/v1/member/runs/{run_id}/stop"; request: VersionInput; response: stopRunResponse }
  "retryRun": { method: "POST"; path: "/mall-agent/v1/member/runs/{run_id}/retry"; request: VersionInput; response: retryRunResponse }
  "prepareAction": { method: "POST"; path: "/mall-agent/v1/member/conversations/{conversation_id}/actions"; request: ActionPrepareInput; response: prepareActionResponse }
  "getAction": { method: "GET"; path: "/mall-agent/v1/member/actions/{action_id}"; request: unknown; response: getActionResponse }
  "confirmAction": { method: "POST"; path: "/mall-agent/v1/member/actions/{action_id}/confirm"; request: VersionInput; response: confirmActionResponse }
  "cancelAction": { method: "POST"; path: "/mall-agent/v1/member/actions/{action_id}/cancel"; request: VersionInput; response: cancelActionResponse }
  "requestHandoff": { method: "POST"; path: "/mall-agent/v1/member/conversations/{conversation_id}/handoff"; request: HandoffInput; response: requestHandoffResponse }
  "withdrawHandoff": { method: "DELETE"; path: "/mall-agent/v1/member/conversations/{conversation_id}/handoff"; request: VersionInput; response: withdrawHandoffResponse }
  "setWaitingAI": { method: "POST"; path: "/mall-agent/v1/member/conversations/{conversation_id}/waiting-ai-mode"; request: WaitingAIInput; response: setWaitingAIResponse }
  "uploadMemberAttachment": { method: "POST"; path: "/mall-agent/v1/member/attachments"; request: unknown; response: uploadMemberAttachmentResponse }
  "readMemberAttachment": { method: "GET"; path: "/mall-agent/v1/member/attachments/{attachment_id}/content"; request: unknown; response: unknown }
  "transcribeAudio": { method: "POST"; path: "/mall-agent/v1/member/audio/transcriptions"; request: TranscriptionInput; response: transcribeAudioResponse }
  "synthesizeSpeech": { method: "POST"; path: "/mall-agent/v1/member/messages/{message_id}/speech"; request: SpeechInput; response: synthesizeSpeechResponse }
  "submitFeedback": { method: "POST"; path: "/mall-agent/v1/member/messages/{message_id}/feedback"; request: FeedbackInput; response: submitFeedbackResponse }
  "readFeedback": { method: "GET"; path: "/mall-agent/v1/member/messages/{message_id}/feedback"; request: unknown; response: readFeedbackResponse }
  "searchProducts": { method: "GET"; path: "/mall-agent/v1/member/products"; request: unknown; response: searchProductsResponse }
  "getProduct": { method: "GET"; path: "/mall-agent/v1/member/products/{product_id}"; request: unknown; response: getProductResponse }
  "getCheckoutOptions": { method: "POST"; path: "/mall-agent/v1/member/checkout-options"; request: CheckoutSelection; response: getCheckoutOptionsResponse }
  "listMemberOrders": { method: "GET"; path: "/mall-agent/v1/member/orders"; request: unknown; response: listMemberOrdersResponse }
  "getMemberOrder": { method: "GET"; path: "/mall-agent/v1/member/orders/{order_id}"; request: unknown; response: getMemberOrderResponse }
  "getMemberLogistics": { method: "GET"; path: "/mall-agent/v1/member/orders/{order_id}/logistics"; request: unknown; response: getMemberLogisticsResponse }
  "listOrderReturns": { method: "GET"; path: "/mall-agent/v1/member/orders/{order_id}/returns"; request: unknown; response: listOrderReturnsResponse }
  "checkReturnEligibility": { method: "GET"; path: "/mall-agent/v1/member/orders/{order_id}/items/{order_item_id}/return-eligibility"; request: unknown; response: checkReturnEligibilityResponse }
  "readCitation": { method: "GET"; path: "/mall-agent/v1/member/citations/{version_id}/chunks/{chunk_id}"; request: unknown; response: readCitationResponse }
  "adminMe": { method: "GET"; path: "/mall-agent/v1/admin/me"; request: unknown; response: adminMeResponse }
  "adminListConversations": { method: "GET"; path: "/mall-agent/v1/admin/conversations"; request: unknown; response: adminListConversationsResponse }
  "adminConversationSnapshot": { method: "GET"; path: "/mall-agent/v1/admin/conversations/{conversation_id}"; request: unknown; response: adminConversationSnapshotResponse }
  "adminListMessages": { method: "GET"; path: "/mall-agent/v1/admin/conversations/{conversation_id}/messages"; request: unknown; response: adminListMessagesResponse }
  "sendStaffMessage": { method: "POST"; path: "/mall-agent/v1/admin/conversations/{conversation_id}/messages"; request: StaffMessageInput; response: sendStaffMessageResponse }
  "getStaffBusinessContext": { method: "GET"; path: "/mall-agent/v1/admin/conversations/{conversation_id}/context"; request: unknown; response: getStaffBusinessContextResponse }
  "listStaffNotes": { method: "GET"; path: "/mall-agent/v1/admin/conversations/{conversation_id}/notes"; request: unknown; response: listStaffNotesResponse }
  "createStaffNote": { method: "POST"; path: "/mall-agent/v1/admin/conversations/{conversation_id}/notes"; request: NoteInput; response: createStaffNoteResponse }
  "adminReadCursor": { method: "POST"; path: "/mall-agent/v1/admin/conversations/{conversation_id}/read"; request: ReadInput; response: adminReadCursorResponse }
  "adminEvents": { method: "GET"; path: "/mall-agent/v1/admin/events"; request: unknown; response: unknown }
  "listHandoffs": { method: "GET"; path: "/mall-agent/v1/admin/handoffs"; request: unknown; response: listHandoffsResponse }
  "claimHandoff": { method: "POST"; path: "/mall-agent/v1/admin/handoffs/{handoff_id}/claim"; request: VersionInput; response: claimHandoffResponse }
  "transferHandoff": { method: "POST"; path: "/mall-agent/v1/admin/handoffs/{handoff_id}/transfer"; request: TransferInput; response: transferHandoffResponse }
  "closeHandoff": { method: "POST"; path: "/mall-agent/v1/admin/handoffs/{handoff_id}/close"; request: CloseInput; response: closeHandoffResponse }
  "listStaff": { method: "GET"; path: "/mall-agent/v1/admin/staff"; request: unknown; response: listStaffResponse }
  "setPresence": { method: "PUT"; path: "/mall-agent/v1/admin/presence"; request: PresenceInput; response: setPresenceResponse }
  "presenceHeartbeat": { method: "POST"; path: "/mall-agent/v1/admin/presence/heartbeat"; request: Empty; response: presenceHeartbeatResponse }
  "updateStaffProfile": { method: "PATCH"; path: "/mall-agent/v1/admin/staff/{staff_id}"; request: StaffProfileInput; response: updateStaffProfileResponse }
  "uploadStaffAttachment": { method: "POST"; path: "/mall-agent/v1/admin/attachments"; request: unknown; response: uploadStaffAttachmentResponse }
  "readStaffAttachment": { method: "GET"; path: "/mall-agent/v1/admin/attachments/{attachment_id}/content"; request: unknown; response: unknown }
  "listKBDocuments": { method: "GET"; path: "/mall-agent/v1/admin/kb/documents"; request: unknown; response: listKBDocumentsResponse }
  "createKBDocument": { method: "POST"; path: "/mall-agent/v1/admin/kb/documents"; request: KBInput; response: createKBDocumentResponse }
  "getKBDocument": { method: "GET"; path: "/mall-agent/v1/admin/kb/documents/{document_id}"; request: unknown; response: getKBDocumentResponse }
  "updateKBDocument": { method: "PATCH"; path: "/mall-agent/v1/admin/kb/documents/{document_id}"; request: KBUpdate; response: updateKBDocumentResponse }
  "listKBVersions": { method: "GET"; path: "/mall-agent/v1/admin/kb/documents/{document_id}/versions"; request: unknown; response: listKBVersionsResponse }
  "createKBVersion": { method: "POST"; path: "/mall-agent/v1/admin/kb/documents/{document_id}/versions"; request: KBVersionInput; response: createKBVersionResponse }
  "getKBVersion": { method: "GET"; path: "/mall-agent/v1/admin/kb/versions/{version_id}"; request: unknown; response: getKBVersionResponse }
  "parseKBVersion": { method: "POST"; path: "/mall-agent/v1/admin/kb/versions/{version_id}/parse"; request: VersionInput; response: parseKBVersionResponse }
  "previewKBVersion": { method: "GET"; path: "/mall-agent/v1/admin/kb/versions/{version_id}/preview"; request: unknown; response: previewKBVersionResponse }
  "editKBPreview": { method: "PATCH"; path: "/mall-agent/v1/admin/kb/versions/{version_id}/preview"; request: KBPreviewUpdate; response: editKBPreviewResponse }
  "buildKBVersion": { method: "POST"; path: "/mall-agent/v1/admin/kb/versions/{version_id}/build"; request: VersionInput; response: buildKBVersionResponse }
  "publishKBVersion": { method: "POST"; path: "/mall-agent/v1/admin/kb/documents/{document_id}/publish"; request: KBPublishInput; response: publishKBVersionResponse }
  "setKBEnabled": { method: "PUT"; path: "/mall-agent/v1/admin/kb/documents/{document_id}/enabled"; request: KBEnabledInput; response: setKBEnabledResponse }
  "listJobs": { method: "GET"; path: "/mall-agent/v1/admin/jobs"; request: unknown; response: listJobsResponse }
  "getJob": { method: "GET"; path: "/mall-agent/v1/admin/jobs/{job_id}"; request: unknown; response: getJobResponse }
  "retryJob": { method: "POST"; path: "/mall-agent/v1/admin/jobs/{job_id}/retry"; request: VersionInput; response: retryJobResponse }
  "listModelProfiles": { method: "GET"; path: "/mall-agent/v1/admin/model-profiles"; request: unknown; response: listModelProfilesResponse }
  "createModelProfile": { method: "POST"; path: "/mall-agent/v1/admin/model-profiles"; request: ModelProfileCreate; response: createModelProfileResponse }
  "getModelRegistry": { method: "GET"; path: "/mall-agent/v1/admin/model-registry"; request: unknown; response: getModelRegistryResponse }
  "testModelProfile": { method: "POST"; path: "/mall-agent/v1/admin/model-profiles/{profile_id}/test"; request: VersionInput; response: testModelProfileResponse }
  "updateModelProfile": { method: "PATCH"; path: "/mall-agent/v1/admin/model-profiles/{profile_id}"; request: ModelProfileUpdate; response: updateModelProfileResponse }
  "activateModelProfile": { method: "POST"; path: "/mall-agent/v1/admin/model-profiles/{profile_id}/activate"; request: ModelActivationInput; response: activateModelProfileResponse }
  "listPrompts": { method: "GET"; path: "/mall-agent/v1/admin/prompt-versions"; request: unknown; response: listPromptsResponse }
  "createPrompt": { method: "POST"; path: "/mall-agent/v1/admin/prompt-versions"; request: PromptInput; response: createPromptResponse }
  "publishPrompt": { method: "POST"; path: "/mall-agent/v1/admin/prompt-versions/{prompt_id}/publish"; request: VersionInput; response: publishPromptResponse }
  "getSettings": { method: "GET"; path: "/mall-agent/v1/admin/settings"; request: unknown; response: getSettingsResponse }
  "updateSettings": { method: "PUT"; path: "/mall-agent/v1/admin/settings"; request: SettingsInput; response: updateSettingsResponse }
  "previewRetention": { method: "POST"; path: "/mall-agent/v1/admin/retention/preview"; request: RetentionPreviewInput; response: previewRetentionResponse }
  "executeRetention": { method: "POST"; path: "/mall-agent/v1/admin/retention/execute"; request: RetentionExecuteInput; response: executeRetentionResponse }
  "getStatistics": { method: "GET"; path: "/mall-agent/v1/admin/statistics"; request: unknown; response: getStatisticsResponse }
  "listUnknownModelUsage": { method: "GET"; path: "/mall-agent/v1/admin/model-usage/unknown"; request: unknown; response: listUnknownModelUsageResponse }
  "reconcileModelUsage": { method: "POST"; path: "/mall-agent/v1/admin/model-usage/{call_id}/reconcile"; request: UsageSettlement; response: reconcileModelUsageResponse }
  "listUnresolvedActions": { method: "GET"; path: "/mall-agent/v1/admin/actions/unresolved"; request: unknown; response: listUnresolvedActionsResponse }
  "resumeActionReconciliation": { method: "POST"; path: "/mall-agent/v1/admin/actions/{action_id}/reconcile"; request: ActionRecoveryInput; response: resumeActionReconciliationResponse }
  "listAuditLogs": { method: "GET"; path: "/mall-agent/v1/admin/audit-logs"; request: unknown; response: listAuditLogsResponse }
  "verifyBackup": { method: "POST"; path: "/mall-agent/v1/admin/backups/verify"; request: BackupVerifyInput; response: verifyBackupResponse }
  "listBackups": { method: "GET"; path: "/mall-agent/v1/admin/backups"; request: unknown; response: listBackupsResponse }
  "createBackup": { method: "POST"; path: "/mall-agent/v1/admin/backups"; request: BackupCreateInput; response: createBackupResponse }
  "updateBackupPolicy": { method: "PUT"; path: "/mall-agent/v1/admin/backups/policy"; request: BackupPolicyInput; response: updateBackupPolicyResponse }
  "bridgeMemberIdentity": { method: "GET"; path: "/mall-portal/agent-bridge/v1/identity"; request: unknown; response: bridgeMemberIdentityResponse }
  "bridgeProducts": { method: "GET"; path: "/mall-portal/agent-bridge/v1/products"; request: unknown; response: bridgeProductsResponse }
  "bridgeProductDetail": { method: "GET"; path: "/mall-portal/agent-bridge/v1/products/{product_id}"; request: unknown; response: bridgeProductDetailResponse }
  "bridgeCheckoutOptions": { method: "POST"; path: "/mall-portal/agent-bridge/v1/checkout-options"; request: CheckoutSelection; response: bridgeCheckoutOptionsResponse }
  "bridgeCart": { method: "GET"; path: "/mall-portal/agent-bridge/v1/cart"; request: unknown; response: bridgeCartResponse }
  "bridgeQuote": { method: "POST"; path: "/mall-portal/agent-bridge/v1/quotes"; request: ActionParameters; response: bridgeQuoteResponse }
  "executeBridgeOperation": { method: "POST"; path: "/mall-portal/agent-bridge/v1/operations"; request: ExecuteOperationInput; response: executeBridgeOperationResponse }
  "getBridgeOperation": { method: "GET"; path: "/mall-portal/agent-bridge/v1/operations/{operation_id}"; request: unknown; response: getBridgeOperationResponse }
  "bridgeOrders": { method: "GET"; path: "/mall-portal/agent-bridge/v1/orders"; request: unknown; response: bridgeOrdersResponse }
  "bridgeOrder": { method: "GET"; path: "/mall-portal/agent-bridge/v1/orders/{order_id}"; request: unknown; response: bridgeOrderResponse }
  "bridgeLogistics": { method: "GET"; path: "/mall-portal/agent-bridge/v1/orders/{order_id}/logistics"; request: unknown; response: bridgeLogisticsResponse }
  "bridgeReturns": { method: "GET"; path: "/mall-portal/agent-bridge/v1/orders/{order_id}/returns"; request: unknown; response: bridgeReturnsResponse }
  "bridgeReturnEligibility": { method: "GET"; path: "/mall-portal/agent-bridge/v1/orders/{order_id}/items/{order_item_id}/return-eligibility"; request: unknown; response: bridgeReturnEligibilityResponse }
  "bridgeStaffIdentity": { method: "GET"; path: "/mall-admin/agent-bridge/v1/identity"; request: unknown; response: bridgeStaffIdentityResponse }
  "bridgeStaffContext": { method: "POST"; path: "/mall-admin/agent-bridge/v1/business-context"; request: StaffContextInput; response: bridgeStaffContextResponse }
  "updateLogistics": { method: "PUT"; path: "/mall-admin/agent-bridge/v1/orders/{order_id}/logistics"; request: LogisticsInput; response: updateLogisticsResponse }
  "getStaffLogistics": { method: "GET"; path: "/mall-admin/agent-bridge/v1/orders/{order_id}/logistics"; request: unknown; response: getStaffLogisticsResponse }
  "createLogisticsNode": { method: "POST"; path: "/mall-admin/agent-bridge/v1/shipments/{shipment_id}/events"; request: LogisticsNodeInput; response: createLogisticsNodeResponse }
  "updateLogisticsNode": { method: "PATCH"; path: "/mall-admin/agent-bridge/v1/shipments/{shipment_id}/events/{event_id}"; request: LogisticsNodeUpdate; response: updateLogisticsNodeResponse }
  "bridgeStaffReturns": { method: "GET"; path: "/mall-admin/agent-bridge/v1/returns"; request: unknown; response: bridgeStaffReturnsResponse }
  "bridgeReturnAddresses": { method: "GET"; path: "/mall-admin/agent-bridge/v1/return-addresses"; request: unknown; response: bridgeReturnAddressesResponse }
  "adminListReturns": { method: "GET"; path: "/mall-agent/v1/admin/returns"; request: unknown; response: adminListReturnsResponse }
  "adminReturnAddresses": { method: "GET"; path: "/mall-agent/v1/admin/return-addresses"; request: unknown; response: adminReturnAddressesResponse }
  "bridgeListReturnRules": { method: "GET"; path: "/mall-admin/agent-bridge/v1/return-rules"; request: unknown; response: bridgeListReturnRulesResponse }
  "bridgePublishReturnRule": { method: "POST"; path: "/mall-admin/agent-bridge/v1/return-rules"; request: ReturnRuleInput; response: bridgePublishReturnRuleResponse }
  "adminListReturnRules": { method: "GET"; path: "/mall-agent/v1/admin/return-rules"; request: unknown; response: adminListReturnRulesResponse }
  "adminPublishReturnRule": { method: "POST"; path: "/mall-agent/v1/admin/return-rules"; request: ReturnRuleInput; response: adminPublishReturnRuleResponse }
  "reviewReturn": { method: "POST"; path: "/mall-admin/agent-bridge/v1/returns/{return_id}/review"; request: ReturnReviewInput; response: reviewReturnResponse }
  "receiveReturn": { method: "POST"; path: "/mall-admin/agent-bridge/v1/returns/{return_id}/receive"; request: ReturnReceiveInput; response: receiveReturnResponse }
  "simulateRefund": { method: "POST"; path: "/mall-admin/agent-bridge/v1/returns/{return_id}/simulate-refund"; request: ReturnRefundInput; response: simulateRefundResponse }
  "getStaffReturn": { method: "GET"; path: "/mall-admin/agent-bridge/v1/returns/{return_id}"; request: unknown; response: getStaffReturnResponse }
  "adminUpdateLogistics": { method: "PUT"; path: "/mall-agent/v1/admin/orders/{order_id}/logistics"; request: LogisticsInput; response: adminUpdateLogisticsResponse }
  "adminReadLogistics": { method: "GET"; path: "/mall-agent/v1/admin/orders/{order_id}/logistics"; request: unknown; response: adminReadLogisticsResponse }
  "adminCreateLogisticsNode": { method: "POST"; path: "/mall-agent/v1/admin/shipments/{shipment_id}/events"; request: LogisticsNodeInput; response: adminCreateLogisticsNodeResponse }
  "adminEditLogisticsNode": { method: "PATCH"; path: "/mall-agent/v1/admin/shipments/{shipment_id}/events/{event_id}"; request: LogisticsNodeUpdate; response: adminEditLogisticsNodeResponse }
  "adminReadReturn": { method: "GET"; path: "/mall-agent/v1/admin/returns/{return_id}"; request: unknown; response: adminReadReturnResponse }
  "adminReviewReturn": { method: "POST"; path: "/mall-agent/v1/admin/returns/{return_id}/review"; request: ReturnReviewInput; response: adminReviewReturnResponse }
  "adminReceiveReturn": { method: "POST"; path: "/mall-agent/v1/admin/returns/{return_id}/receive"; request: ReturnReceiveInput; response: adminReceiveReturnResponse }
  "adminSimulateRefund": { method: "POST"; path: "/mall-agent/v1/admin/returns/{return_id}/simulate-refund"; request: ReturnRefundInput; response: adminSimulateRefundResponse }
  "listModelProfileVersions": { method: "GET"; path: "/mall-agent/v1/admin/model-profiles/{profile_id}/versions"; request: unknown; response: listModelProfileVersionsResponse }
  "reconcileBridgeOperation": { method: "POST"; path: "/mall-portal/agent-bridge/v1/operations/{operation_id}/reconcile"; request: ReconcileOperationInput; response: reconcileBridgeOperationResponse }
}

export type AgentOperationId = keyof AgentOperations
