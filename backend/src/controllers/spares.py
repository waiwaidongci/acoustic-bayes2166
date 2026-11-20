from ..services import spares
class SparesController:
    get=staticmethod(spares.for_run)
    create=staticmethod(spares.create_version)
    retry=staticmethod(spares.retry_current)
