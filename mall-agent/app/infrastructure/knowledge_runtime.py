from contextlib import asynccontextmanager

from app.application.knowledge import Knowledge
from app.application.retrieval import AuthorizedRetrieval
from app.infrastructure.aliyun_models import AliyunModels
from app.infrastructure.knowledge_files import KnowledgeFiles
from app.infrastructure.knowledge_index import KnowledgeIndexes


class KnowledgeRuntime:
    def __init__(self, db, settings, jobs, cursors, objects, ledger, frozen=False):
        self.db, self.settings, self.objects, self.ledger = db, settings, objects, ledger
        self.files = KnowledgeFiles(db, objects)
        self.models = AliyunModels(settings, ledger)
        self.indexes = KnowledgeIndexes(settings)
        self.knowledge = Knowledge(db, jobs, cursors, self.files, ledger, self.models, self.indexes)
        self.retrieval = AuthorizedRetrieval(db, ledger, self.models, self.indexes)
        if not frozen:
            self.knowledge.runtime_factory = self.frozen_runtime
            self.retrieval.runtime_factory = self.frozen_runtime
        if jobs:
            self.knowledge.register(jobs.handlers)

    @asynccontextmanager
    async def frozen_runtime(self):
        ledger = await self.ledger.for_snapshot(await self.ledger.capture())
        runtime = KnowledgeRuntime(self.db, ledger.settings, None, None, self.objects, ledger, frozen=True)
        try:
            yield runtime
        finally:
            await runtime.close()

    async def close(self):
        await self.models.close()
        self.indexes.close()
