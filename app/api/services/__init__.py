from .prices import PricesService
from .audit import AuditService, AuditTypes
from .assessments import AssessmentsService
from .briefs import BriefsService
from .suppliers import SuppliersService
from .lots import LotsService
from .brief_responses import BriefResponsesService
from .users import UsersService

prices = PricesService()
audit_service = AuditService()
audit_types = AuditTypes
assessments = AssessmentsService()
briefs = BriefsService()
suppliers = SuppliersService()
lots_service = LotsService()
brief_responses_service = BriefResponsesService()
users = UsersService()
