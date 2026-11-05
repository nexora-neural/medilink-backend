# ruff: noqa: F401

from app.modules.audit import models as _audit_models
from app.modules.clinical_records import models as _clinical_records_models
from app.modules.consent import models as _consent_models
from app.modules.consultations import models as _consultations_models
from app.modules.dispensing import models as _dispensing_models
from app.modules.documents import models as _documents_models
from app.modules.fhir import models as _fhir_models
from app.modules.identity import models as _identity_models
from app.modules.identity.models import Role, User, UserRole
from app.modules.integrations import models as _integrations_models
from app.modules.laboratory import models as _laboratory_models
from app.modules.measurements import models as _measurements_models
from app.modules.medication import models as _medication_models
from app.modules.notifications import models as _notifications_models
from app.modules.organizations import models as _organizations_models
from app.modules.organizations.models import Organization, OrganizationMembership
from app.modules.prescriptions import core_models as _prescription_core_models
from app.modules.prescriptions import models as _prescriptions_models
from app.modules.prescriptions import review_models as _prescription_review_models
from app.modules.profiles import models as _profiles_models
from app.modules.vaccinations import models as _vaccinations_models
from app.modules.verification import models as _verification_models
