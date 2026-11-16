from app.application.actions import Actions
from app.application.attachments import Attachments
from app.application.conversations import Conversations
from app.application.extensions import Extensions
from app.application.feedback import Feedback
from app.application.governance import Governance
from app.application.handoffs import Handoffs
from app.application.jobs import Jobs
from app.application.media import Media
from app.application.model_admin import ModelAdministration
from app.application.orders import OrderBusiness
from app.application.runs import Runs
from app.application.runtime_check import runtime_handlers
from app.framework import ExternalBoundary, Framework
from app.infrastructure.cursors import Cursors
from app.infrastructure.java_bridge import JavaBridge
from app.infrastructure.knowledge_runtime import KnowledgeRuntime
from app.infrastructure.model_ledger import ModelLedger
from app.infrastructure.postgres.database import Database, append_events
from app.infrastructure.private_objects import PrivateObjects
from app.infrastructure.runtime_auth import RuntimeAuth
from app.infrastructure.staff_auth import StaffAuthorization
from app.infrastructure.wakeups import NotificationBindings


class Services:
    def __init__(self, settings):
        self.db = Database(
            settings.database_url.get_secret_value(),
            performance_timing_enabled=settings.performance_timing_enabled,
        )
        self.bridge = JavaBridge(settings)
        self.auth = RuntimeAuth(settings.auth_ttl_seconds)
        self.cursors = Cursors(settings.cursor_secret.get_secret_value())
        self.models = ModelLedger(self.db, settings)
        self.conversations = Conversations(
            self.db, self.auth, self.cursors, settings, self.models, self.bridge
        )
        self.runs = Runs(self.db, settings)
        self.feedback = Feedback(self.db)
        self.objects = PrivateObjects(settings) if settings.attachments_enabled else None
        self.attachments = Attachments(self.db, self.objects, settings)
        self.media = Media(self.db, self.attachments, self.models, settings)
        self.extensions = Extensions()
        if settings.governance_enabled:
            self.extensions.governance.implementation = Governance(
                self.db, settings.backups_enabled, settings.observability_enabled
            )
            self.extensions.observability.implementation = self.extensions.governance.implementation
        if settings.staff_bridge_secret.get_secret_value():
            self.extensions.staff_identity.implementation = StaffAuthorization(self.bridge)
        if settings.handoffs_enabled:
            self.extensions.handoffs.implementation = Handoffs(
                self.db, self.conversations, self.cursors, settings, self.bridge
            )
        if settings.actions_enabled:
            self.extensions.actions.implementation = Actions(self.db, self.bridge, settings)
            self.extensions.transactions.implementation = self.bridge
            if settings.orders_enabled:
                self.extensions.after_sale.implementation = OrderBusiness(self.bridge)
        self.job_handlers = runtime_handlers(self.db, settings)
        self.jobs = Jobs(self.db, settings, self.job_handlers)
        if settings.backups_enabled:
            from app.application.backups import Backups

            self.extensions.backups.implementation = Backups(self.db, self.jobs)
        if settings.retention_enabled:
            from app.application.retention import Retention

            self.extensions.retention.implementation = Retention(self.db, settings, self.jobs)
        self.extensions.model_admin.implementation = ModelAdministration(
            self.db, self.models, self.jobs, self.cursors, settings
        )
        self.knowledge_runtime = None
        if settings.knowledge_enabled:
            self.knowledge_runtime = KnowledgeRuntime(
                self.db, settings, self.jobs, self.cursors, self.objects, self.models
            )
            self.knowledge_runtime.retrieval.bridge = self.bridge
            self.extensions.knowledge.implementation = self.knowledge_runtime.knowledge
            self.extensions.retrieval.implementation = self.knowledge_runtime.retrieval
        self.notifications = NotificationBindings(settings)
        self.framework = Framework(
            {
                "bridge": self.bridge,
                "conversations": self.conversations,
                "events": append_events,
                "runs": self.runs,
                "tools": self.bridge,
                "attachments": self.attachments,
                "models": self.models,
                "jobs": self.jobs,
                "buyer_ui": ExternalBoundary(),
                "staff_ui": ExternalBoundary(),
                "runtime": settings,
                **vars(self.extensions),
            }
        )

    async def close(self):
        if self.knowledge_runtime:
            await self.knowledge_runtime.close()
        await self.notifications.close()
        if self.objects:
            self.objects.close()
        await self.bridge.client.aclose()
        await self.db.engine.dispose()
