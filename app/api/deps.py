"""Request dependencies: session, caller, organization context, guards.

This is where the isolation model of §6.1 is wired into a request, and the only place services
are constructed. `fastapi-rules` requires exactly one `get_*_service` provider per service and
all of them here, so a route never builds a service itself and never sees a `Session`.

The order of resolution matters and is deliberate:

1. :func:`get_db` opens a session and guarantees rollback of anything left open.
2. :func:`get_current_user` verifies the bearer token and loads an **active** account.
3. :func:`get_org_context` resolves the organization **from the path** and the caller's active
   grants, producing an :class:`~app.core.context.OrgContext`. A request body never
   contributes an organization id (§7 of the brief).
4. :func:`require` checks one permission against that context.

A route that forgets step 4 still cannot read another tenant's data, because step 3 has already
bound the organization and the repository layer filters on it. That is the point of having four
layers rather than one.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Iterator
from typing import Annotated

from fastapi import Depends, Header, Path, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.audit.writer import AuditWriter
from app.core.config import Settings, get_settings
from app.core.context import ActorContext, OrgContext, RequestMetadata
from app.core.crypto import BlindIndexer, FieldCipher
from app.core.errors import Unauthenticated
from app.core.logging import bind_request_context, current_request_id
from app.core.permissions import Permission
from app.core.security import (
    CompositeTokenVerifier,
    LocalTokenIssuer,
    OidcTokenVerifier,
    TokenVerifier,
)
from app.db.session import get_session_factory
from app.integrations.keys import EnvKeyProvider
from app.repositories.attendance import (
    AttendancePolicyRepository,
    AttendancePolicyVersionRepository,
    AttendanceRecordRepository,
    PunchRepository,
    RegularizationActionRepository,
    RegularizationItemRepository,
    RegularizationRequestRepository,
    RevisionRepository,
    ShiftAssignmentRepository,
    ShiftRepository,
)
from app.repositories.audit import AuditLogRepository
from app.repositories.bank import EmployeeBankAccountRepository
from app.repositories.branch import BranchRepository
from app.repositories.document import (
    DocumentCategoryRepository,
    DocumentMimeTypeRepository,
    EmployeeDocumentRepository,
)
from app.repositories.employee import (
    EmployeeAddressRepository,
    EmployeeJobHistoryRepository,
    EmployeeRepository,
    EmployeeStatusHistoryRepository,
)
from app.repositories.employee_code import (
    EmployeeCodeRegistryRepository,
    EmployeeIdSequenceRepository,
    IdRuleRepository,
)
from app.repositories.identifier import EmployeeIdentifierRepository
from app.repositories.leave import (
    LeaveActionRepository,
    LeaveApplicationRepository,
    LeaveBalanceRepository,
    LeavePolicyRepository,
    LeavePolicyVersionRepository,
    LeaveSlotRepository,
    LeaveTransactionRepository,
    LeaveTypeRepository,
)
from app.repositories.master import (
    CustomStatusRepository,
    DepartmentRepository,
    GradeRepository,
    LookupRepository,
    MasterReferenceChecker,
)
from app.repositories.onboarding import (
    AcknowledgementRepository,
    InstanceEventRepository,
    InstanceRepository,
    SubmissionRepository,
    TaskDependencyRepository,
    TaskDocumentRepository,
    TaskEventRepository,
    TaskRepository,
    TemplateDependencyRepository,
    TemplateRepository,
    TemplateTaskRepository,
    TemplateVersionRepository,
)
from app.repositories.organization import (
    IdentifierPolicyRepository,
    NominationConfigRepository,
    OrganizationGroupRepository,
    OrganizationRegistrationRepository,
    OrganizationRepository,
    OrganizationSettingsRepository,
)
from app.repositories.platform import (
    AccessGrantRepository,
    IdempotencyRepository,
    LoginAttemptRepository,
    OrganizationRolePermissionRepository,
    PermissionRepository,
    RefreshTokenRepository,
    RoleRepository,
    UserRepository,
)
from app.repositories.position import (
    CompetencyRepository,
    HeadcountChangeRequestRepository,
    HeadcountPlanRepository,
    HeadcountReservationRepository,
    JobCompetencyRepository,
    JobFamilyRepository,
    JobRepository,
    PositionAssignmentRepository,
    PositionCompetencyRepository,
    PositionRepository,
    PositionStatusHistoryRepository,
)
from app.repositories.privacy import (
    ErasureRequestRepository,
    PrivacyNoticeRepository,
    RetentionBatchRepository,
)
from app.repositories.profile import (
    EducationRepository,
    FamilyRepository,
    NominationRepository,
    PreviousEmploymentDocumentRepository,
    PreviousEmploymentRepository,
)
from app.repositories.reference import StateRepository
from app.repositories.salary import (
    AssignmentComponentRepository,
    SalaryAssignmentRepository,
    SalaryComponentRepository,
    SalaryRevisionHistoryRepository,
    SalaryTemplateComponentRepository,
    SalaryTemplateRepository,
    SalaryTemplateVersionRepository,
)
from app.repositories.schedule import (
    BranchCalendarAssignmentRepository,
    EmployeeBranchResolver,
    HolidayCalendarRepository,
    HolidayRepository,
    WeeklyOffPatternRepository,
)
from app.schemas.common import CursorCodec, parse_if_match
from app.services.access import AccessService, PermissionCache
from app.services.approvals import ApprovalRouter
from app.services.attendance import AttendanceService
from app.services.attendance_config import AttendanceConfigService
from app.services.attendance_records import AttendanceLeaveEffects, RecordEngine
from app.services.attendance_regularization import RegularizationService
from app.services.audit_query import AuditQueryService
from app.services.auth import AuthService
from app.services.bank import BankAccountService
from app.services.capacity import CapacityCalculator
from app.services.catalog import CatalogueService
from app.services.competencies import CompetencyService
from app.services.documents import DocumentService
from app.services.employee_codes import EmployeeCodeService
from app.services.employees import EmployeeService
from app.services.headcount import HeadcountService
from app.services.identifiers import IdentifierService
from app.services.job_catalogue import JobCatalogueService
from app.services.leave_applications import LeaveApplicationService
from app.services.leave_config import LeaveConfigService
from app.services.leave_ledger import LeaveLedger, LeaveLedgerService
from app.services.lifecycle import LifecycleHooks
from app.services.onboarding import OnboardingService
from app.services.onboarding_submissions import SubmissionService
from app.services.onboarding_templates import TemplateService
from app.services.org_config import OrganizationConfigService
from app.services.organizations import OrganizationService
from app.services.position_assignments import PositionAssignmentService
from app.services.positions import PositionService
from app.services.privacy import PrivacyService
from app.services.profile_records import ProfileRecordService
from app.services.salary import SalaryService
from app.services.schedule import ScheduleService
from app.services.transfers import TransferService
from app.storage.local import LocalStorage, SignedUrlSigner

bearer_scheme = HTTPBearer(auto_error=False, description="OAuth 2.0 / OIDC bearer token")
"""``auto_error=False`` so a missing credential produces our own §24.2 envelope with
``UNAUTHENTICATED`` rather than FastAPI's ``{"detail": "Not authenticated"}``."""


# --------------------------------------------------------------------------- process scope


def get_app_settings(request: Request) -> Settings:
    """The settings of the application handling this request.

    Read from ``app.state`` rather than the process singleton, so an application built
    with explicit settings — a test, a job, a second app in one process — has its
    dependencies use *those* settings. Reaching for ``get_settings()`` unconditionally
    meant a test app validated the developer's ``.env`` on every request and failed on it.
    """
    configured = getattr(request.app.state, "settings", None)
    if isinstance(configured, Settings):
        return configured
    return get_settings()


class _PermissionCacheHolder:
    """Lazily builds the process permission cache from settings.

    A holder rather than a module global so the TTL comes from configuration (it must be ≤60 s
    per §5.5) instead of a hardcoded constant, and so tests can reset it. Lock-guarded because
    endpoints are served from a threadpool (ADR-001).
    """

    __slots__ = ("_cache", "_lock")

    def __init__(self) -> None:
        self._cache: PermissionCache | None = None
        self._lock = threading.Lock()

    def get(self, ttl_seconds: int) -> PermissionCache:
        with self._lock:
            if self._cache is None:
                self._cache = PermissionCache(ttl_seconds=ttl_seconds)
            return self._cache

    def reset(self) -> None:
        with self._lock:
            self._cache = None


_permission_cache_holder = _PermissionCacheHolder()


def get_permission_cache(
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> PermissionCache:
    """The process-wide permission cache (§5.5).

    One instance per process, because its whole purpose is to avoid re-resolving grants on
    every request. Invalidated eagerly by :class:`AccessService` on any grant change, so the
    TTL is a worst case for changes made on another worker rather than normal latency.
    """
    return _permission_cache_holder.get(settings.permission_cache_ttl_seconds)


def reset_permission_cache() -> None:
    """Drop the process cache. For tests and for a deliberate operational flush."""
    _permission_cache_holder.reset()


# --------------------------------------------------------------------------- database


def get_key_provider(
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> EnvKeyProvider:
    """The deployment's key provider.

    Only the environment provider exists in this repository; a KMS deployment supplies its
    own implementation of the same protocol (docs/CONFIGURATION.md).
    """
    return EnvKeyProvider(
        encryption_master_key=settings.decoded_encryption_key(),
        blind_index_master_key=settings.decoded_blind_index_key(),
        key_version=settings.encryption_key_version,
    )


def get_cursor_codec(
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> CursorCodec:
    return CursorCodec(
        secret=settings.secret_key.get_secret_value(),
        ttl_seconds=settings.cursor_ttl_seconds,
    )


def get_field_cipher(keys: Annotated[EnvKeyProvider, Depends(get_key_provider)]) -> FieldCipher:
    return FieldCipher(keys)


def get_blind_indexer(
    keys: Annotated[EnvKeyProvider, Depends(get_key_provider)],
) -> BlindIndexer:
    return BlindIndexer(keys)


def get_db() -> Iterator[Session]:
    """A request-scoped session.

    Does **not** commit: that is the service's `unit_of_work`. The rollback here is a
    safety net for a handler that raised after writing, so a half-finished change cannot leak
    into the next request on this connection.
    """
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


DbSession = Annotated[Session, Depends(get_db)]


# --------------------------------------------------------------------------- repositories


def get_user_repository(db: DbSession) -> UserRepository:
    return UserRepository(db)


def get_role_repository(db: DbSession) -> RoleRepository:
    return RoleRepository(db)


def get_permission_repository(db: DbSession) -> PermissionRepository:
    return PermissionRepository(db)


def get_access_grant_repository(db: DbSession) -> AccessGrantRepository:
    return AccessGrantRepository(db)


def get_override_repository(db: DbSession) -> OrganizationRolePermissionRepository:
    return OrganizationRolePermissionRepository(db)


def get_refresh_token_repository(db: DbSession) -> RefreshTokenRepository:
    return RefreshTokenRepository(db)


def get_login_attempt_repository(db: DbSession) -> LoginAttemptRepository:
    return LoginAttemptRepository(db)


def get_idempotency_repository(db: DbSession) -> IdempotencyRepository:
    return IdempotencyRepository(db)


def get_organization_repository(db: DbSession) -> OrganizationRepository:
    return OrganizationRepository(db)


def get_organization_settings_repository(db: DbSession) -> OrganizationSettingsRepository:
    return OrganizationSettingsRepository(db)


def get_organization_group_repository(db: DbSession) -> OrganizationGroupRepository:
    return OrganizationGroupRepository(db)


def get_branch_repository(db: DbSession, context: CurrentOrg) -> BranchRepository:
    return BranchRepository(db, context.organization_id)


def get_state_repository(db: DbSession) -> StateRepository:
    return StateRepository(db)


def get_registration_repository(
    db: DbSession, context: CurrentOrg
) -> OrganizationRegistrationRepository:
    return OrganizationRegistrationRepository(db, context.organization_id)


def get_identifier_policy_repository(
    db: DbSession, context: CurrentOrg
) -> IdentifierPolicyRepository:
    return IdentifierPolicyRepository(db, context.organization_id)


def get_nomination_config_repository(
    db: DbSession, context: CurrentOrg
) -> NominationConfigRepository:
    return NominationConfigRepository(db, context.organization_id)


def get_audit_writer(db: DbSession) -> AuditWriter:
    """An audit writer bound to **this request's** session.

    Sharing the session is the mechanism behind NFR-10: the audit row and the business change
    are in one transaction, so they commit or roll back together.
    """
    return AuditWriter(db)


# --------------------------------------------------------------------------- services


def get_token_verifier(
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> TokenVerifier:
    """The token verifier for this deployment.

    OIDC is tried first when configured, so a local signing secret can never be used to forge
    a token the identity provider would otherwise have had to sign.
    """
    local = LocalTokenIssuer(
        secret=settings.secret_key.get_secret_value(),
        algorithm=settings.token_algorithm,
        issuer=settings.token_issuer,
        audience=settings.token_audience,
        access_token_ttl_seconds=settings.access_token_ttl_seconds,
    )
    if settings.oidc_enabled:
        oidc = OidcTokenVerifier(
            issuer=str(settings.oidc_issuer),
            audience=str(settings.oidc_audience),
            jwks_url=str(settings.oidc_jwks_url),
            cache_seconds=settings.oidc_jwks_cache_seconds,
        )
        return CompositeTokenVerifier(oidc, local)
    return local


def get_token_issuer(
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> LocalTokenIssuer:
    return LocalTokenIssuer(
        secret=settings.secret_key.get_secret_value(),
        algorithm=settings.token_algorithm,
        issuer=settings.token_issuer,
        audience=settings.token_audience,
        access_token_ttl_seconds=settings.access_token_ttl_seconds,
    )


def get_auth_service(
    db: DbSession,
    users: Annotated[UserRepository, Depends(get_user_repository)],
    refresh_tokens: Annotated[RefreshTokenRepository, Depends(get_refresh_token_repository)],
    login_attempts: Annotated[LoginAttemptRepository, Depends(get_login_attempt_repository)],
    issuer: Annotated[LocalTokenIssuer, Depends(get_token_issuer)],
    audit: Annotated[AuditWriter, Depends(get_audit_writer)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> AuthService:
    return AuthService(
        session=db,
        users=users,
        refresh_tokens=refresh_tokens,
        login_attempts=login_attempts,
        issuer=issuer,
        audit=audit,
        refresh_ttl_hours=settings.refresh_token_ttl_hours,
        access_ttl_seconds=settings.access_token_ttl_seconds,
        local_login_enabled=settings.local_login_enabled,
    )


def get_lookup_repository(db: DbSession, context: CurrentOrg) -> LookupRepository:
    return LookupRepository(db, context.organization_id)


def get_custom_status_repository(db: DbSession, context: CurrentOrg) -> CustomStatusRepository:
    return CustomStatusRepository(db, context.organization_id)


def get_employee_repository(db: DbSession, context: CurrentOrg) -> EmployeeRepository:
    return EmployeeRepository(db, context.organization_id)


def get_id_rule_repository(db: DbSession, context: CurrentOrg) -> IdRuleRepository:
    return IdRuleRepository(db, context.organization_id)


def get_id_sequence_repository(db: DbSession, context: CurrentOrg) -> EmployeeIdSequenceRepository:
    return EmployeeIdSequenceRepository(db, context.organization_id)


def get_code_registry_repository(
    db: DbSession, context: CurrentOrg
) -> EmployeeCodeRegistryRepository:
    return EmployeeCodeRegistryRepository(db, context.organization_id)


def get_audit_log_repository(db: DbSession, context: CurrentOrg) -> AuditLogRepository:
    return AuditLogRepository(db, context.organization_id)


def get_audit_query_service(
    audit_logs: Annotated[AuditLogRepository, Depends(get_audit_log_repository)],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repository)],
) -> AuditQueryService:
    return AuditQueryService(audit_logs=audit_logs, employees=employees)


def get_bank_account_repository(
    db: DbSession, context: CurrentOrg
) -> EmployeeBankAccountRepository:
    return EmployeeBankAccountRepository(db, context.organization_id)


def get_bank_account_service(
    accounts: Annotated[EmployeeBankAccountRepository, Depends(get_bank_account_repository)],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repository)],
    organizations: Annotated[OrganizationRepository, Depends(get_organization_repository)],
    cipher: Annotated[FieldCipher, Depends(get_field_cipher)],
    blind_indexer: Annotated[BlindIndexer, Depends(get_blind_indexer)],
    audit: Annotated[AuditWriter, Depends(get_audit_writer)],
) -> BankAccountService:
    return BankAccountService(
        accounts=accounts,
        employees=employees,
        organizations=organizations,
        cipher=cipher,
        blind_indexer=blind_indexer,
        audit=audit,
    )


def get_privacy_notice_repository(db: DbSession, context: CurrentOrg) -> PrivacyNoticeRepository:
    return PrivacyNoticeRepository(db, context.organization_id)


def get_erasure_repository(db: DbSession, context: CurrentOrg) -> ErasureRequestRepository:
    return ErasureRequestRepository(db, context.organization_id)


def get_retention_batch_repository(db: DbSession, context: CurrentOrg) -> RetentionBatchRepository:
    return RetentionBatchRepository(db, context.organization_id)


def get_privacy_service(
    notices: Annotated[PrivacyNoticeRepository, Depends(get_privacy_notice_repository)],
    erasures: Annotated[ErasureRequestRepository, Depends(get_erasure_repository)],
    batches: Annotated[RetentionBatchRepository, Depends(get_retention_batch_repository)],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repository)],
    addresses: Annotated[EmployeeAddressRepository, Depends(get_employee_address_repository)],
    identifiers: Annotated[EmployeeIdentifierRepository, Depends(get_identifier_repository)],
    bank: Annotated[EmployeeBankAccountRepository, Depends(get_bank_account_repository)],
    documents: Annotated[EmployeeDocumentRepository, Depends(get_employee_document_repository)],
    assignments: Annotated[SalaryAssignmentRepository, Depends(get_salary_assignment_repository)],
    assignment_components: Annotated[
        AssignmentComponentRepository, Depends(get_assignment_component_repository)
    ],
    nominations: Annotated[NominationRepository, Depends(get_nomination_repository)],
    organizations: Annotated[OrganizationRepository, Depends(get_organization_repository)],
    cipher: Annotated[FieldCipher, Depends(get_field_cipher)],
    storage: Annotated[LocalStorage, Depends(get_local_storage)],
    signer: Annotated[SignedUrlSigner, Depends(get_url_signer)],
    audit: Annotated[AuditWriter, Depends(get_audit_writer)],
) -> PrivacyService:
    return PrivacyService(
        notices=notices,
        erasures=erasures,
        batches=batches,
        employees=employees,
        addresses=addresses,
        identifiers=identifiers,
        bank=bank,
        documents=documents,
        assignments=assignments,
        assignment_components=assignment_components,
        nominations=nominations,
        organizations=organizations,
        cipher=cipher,
        storage=storage,
        signer=signer,
        audit=audit,
    )


def get_transfer_service(
    employees: Annotated[EmployeeService, Depends(get_employee_service)],
    organizations: Annotated[OrganizationRepository, Depends(get_organization_repository)],
    cipher: Annotated[FieldCipher, Depends(get_field_cipher)],
    audit: Annotated[AuditWriter, Depends(get_audit_writer)],
    db: DbSession,
) -> TransferService:
    return TransferService(
        session=db,
        target_service=employees,
        organizations=organizations,
        cipher=cipher,
        audit=audit,
    )


def get_salary_component_repository(
    db: DbSession, context: CurrentOrg
) -> SalaryComponentRepository:
    return SalaryComponentRepository(db, context.organization_id)


def get_salary_template_repository(db: DbSession, context: CurrentOrg) -> SalaryTemplateRepository:
    return SalaryTemplateRepository(db, context.organization_id)


def get_salary_version_repository(
    db: DbSession, context: CurrentOrg
) -> SalaryTemplateVersionRepository:
    return SalaryTemplateVersionRepository(db, context.organization_id)


def get_salary_mapping_repository(
    db: DbSession, context: CurrentOrg
) -> SalaryTemplateComponentRepository:
    return SalaryTemplateComponentRepository(db, context.organization_id)


def get_salary_assignment_repository(
    db: DbSession, context: CurrentOrg
) -> SalaryAssignmentRepository:
    return SalaryAssignmentRepository(db, context.organization_id)


def get_assignment_component_repository(db: DbSession) -> AssignmentComponentRepository:
    return AssignmentComponentRepository(db)


def get_salary_history_repository(
    db: DbSession, context: CurrentOrg
) -> SalaryRevisionHistoryRepository:
    return SalaryRevisionHistoryRepository(db, context.organization_id)


def get_salary_service(
    components: Annotated[SalaryComponentRepository, Depends(get_salary_component_repository)],
    templates: Annotated[SalaryTemplateRepository, Depends(get_salary_template_repository)],
    versions: Annotated[SalaryTemplateVersionRepository, Depends(get_salary_version_repository)],
    mappings: Annotated[SalaryTemplateComponentRepository, Depends(get_salary_mapping_repository)],
    assignments: Annotated[SalaryAssignmentRepository, Depends(get_salary_assignment_repository)],
    assignment_components: Annotated[
        AssignmentComponentRepository, Depends(get_assignment_component_repository)
    ],
    history: Annotated[SalaryRevisionHistoryRepository, Depends(get_salary_history_repository)],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repository)],
    organizations: Annotated[OrganizationRepository, Depends(get_organization_repository)],
    settings_repo: Annotated[
        OrganizationSettingsRepository, Depends(get_organization_settings_repository)
    ],
    audit: Annotated[AuditWriter, Depends(get_audit_writer)],
) -> SalaryService:
    return SalaryService(
        components=components,
        templates=templates,
        versions=versions,
        mappings=mappings,
        assignments=assignments,
        assignment_components=assignment_components,
        history=history,
        employees=employees,
        organizations=organizations,
        settings=settings_repo,
        audit=audit,
    )


def get_document_category_repository(
    db: DbSession, context: CurrentOrg
) -> DocumentCategoryRepository:
    return DocumentCategoryRepository(db, context.organization_id)


def get_document_mime_repository(db: DbSession) -> DocumentMimeTypeRepository:
    return DocumentMimeTypeRepository(db)


def get_employee_document_repository(
    db: DbSession, context: CurrentOrg
) -> EmployeeDocumentRepository:
    return EmployeeDocumentRepository(db, context.organization_id)


def get_local_storage(
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> LocalStorage:
    return LocalStorage(settings.storage_local_root)


def get_url_signer(
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> SignedUrlSigner:
    return SignedUrlSigner(settings.secret_key.get_secret_value())


def get_document_service(
    categories: Annotated[DocumentCategoryRepository, Depends(get_document_category_repository)],
    mime_types: Annotated[DocumentMimeTypeRepository, Depends(get_document_mime_repository)],
    documents: Annotated[EmployeeDocumentRepository, Depends(get_employee_document_repository)],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repository)],
    organizations: Annotated[OrganizationRepository, Depends(get_organization_repository)],
    settings_repo: Annotated[
        OrganizationSettingsRepository, Depends(get_organization_settings_repository)
    ],
    cipher: Annotated[FieldCipher, Depends(get_field_cipher)],
    storage: Annotated[LocalStorage, Depends(get_local_storage)],
    signer: Annotated[SignedUrlSigner, Depends(get_url_signer)],
    audit: Annotated[AuditWriter, Depends(get_audit_writer)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> DocumentService:
    return DocumentService(
        categories=categories,
        mime_types=mime_types,
        documents=documents,
        employees=employees,
        organizations=organizations,
        settings=settings_repo,
        cipher=cipher,
        storage=storage,
        signer=signer,
        audit=audit,
        upload_ttl_seconds=settings.upload_url_ttl_seconds,
    )


def get_education_repository(db: DbSession, context: CurrentOrg) -> EducationRepository:
    return EducationRepository(db, context.organization_id)


def get_previous_employment_repository(
    db: DbSession, context: CurrentOrg
) -> PreviousEmploymentRepository:
    return PreviousEmploymentRepository(db, context.organization_id)


def get_previous_employment_document_repository(
    db: DbSession,
) -> PreviousEmploymentDocumentRepository:
    return PreviousEmploymentDocumentRepository(db)


def get_family_repository(db: DbSession, context: CurrentOrg) -> FamilyRepository:
    return FamilyRepository(db, context.organization_id)


def get_nomination_repository(db: DbSession, context: CurrentOrg) -> NominationRepository:
    return NominationRepository(db, context.organization_id)


def get_profile_record_service(
    education: Annotated[EducationRepository, Depends(get_education_repository)],
    previous: Annotated[PreviousEmploymentRepository, Depends(get_previous_employment_repository)],
    previous_documents: Annotated[
        PreviousEmploymentDocumentRepository,
        Depends(get_previous_employment_document_repository),
    ],
    family: Annotated[FamilyRepository, Depends(get_family_repository)],
    nominations: Annotated[NominationRepository, Depends(get_nomination_repository)],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repository)],
    nomination_configs: Annotated[
        NominationConfigRepository, Depends(get_nomination_config_repository)
    ],
    audit: Annotated[AuditWriter, Depends(get_audit_writer)],
) -> ProfileRecordService:
    return ProfileRecordService(
        education=education,
        previous=previous,
        previous_documents=previous_documents,
        family=family,
        nominations=nominations,
        employees=employees,
        nomination_configs=nomination_configs,
        audit=audit,
    )


def get_identifier_repository(db: DbSession, context: CurrentOrg) -> EmployeeIdentifierRepository:
    return EmployeeIdentifierRepository(db, context.organization_id)


def get_identifier_service(
    identifiers: Annotated[EmployeeIdentifierRepository, Depends(get_identifier_repository)],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repository)],
    organizations: Annotated[OrganizationRepository, Depends(get_organization_repository)],
    policies: Annotated[IdentifierPolicyRepository, Depends(get_identifier_policy_repository)],
    cipher: Annotated[FieldCipher, Depends(get_field_cipher)],
    blind_indexer: Annotated[BlindIndexer, Depends(get_blind_indexer)],
    audit: Annotated[AuditWriter, Depends(get_audit_writer)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> IdentifierService:
    return IdentifierService(
        identifiers=identifiers,
        employees=employees,
        organizations=organizations,
        policies=policies,
        cipher=cipher,
        blind_indexer=blind_indexer,
        audit=audit,
        reveal_limit_per_hour=settings.identifier_reveal_rate_per_hour,
    )


def get_employee_address_repository(
    db: DbSession, context: CurrentOrg
) -> EmployeeAddressRepository:
    return EmployeeAddressRepository(db, context.organization_id)


def get_employee_job_history_repository(
    db: DbSession, context: CurrentOrg
) -> EmployeeJobHistoryRepository:
    return EmployeeJobHistoryRepository(db, context.organization_id)


def get_employee_status_history_repository(
    db: DbSession, context: CurrentOrg
) -> EmployeeStatusHistoryRepository:
    return EmployeeStatusHistoryRepository(db, context.organization_id)


def get_capacity_calculator(db: DbSession, context: CurrentOrg) -> CapacityCalculator:
    org = context.organization_id
    return CapacityCalculator(
        positions=PositionRepository(db, org),
        assignments=PositionAssignmentRepository(db, org),
        reservations=HeadcountReservationRepository(db, org),
        plans=HeadcountPlanRepository(db, org),
    )


def get_position_service(
    capacity: Annotated[CapacityCalculator, Depends(get_capacity_calculator)],
    audit: Annotated[AuditWriter, Depends(get_audit_writer)],
    organizations: Annotated[OrganizationRepository, Depends(get_organization_repository)],
    settings: Annotated[
        OrganizationSettingsRepository, Depends(get_organization_settings_repository)
    ],
    db: DbSession,
    context: CurrentOrg,
) -> PositionService:
    org = context.organization_id
    return PositionService(
        positions=PositionRepository(db, org),
        status_history=PositionStatusHistoryRepository(db, org),
        assignments=PositionAssignmentRepository(db, org),
        reservations=HeadcountReservationRepository(db, org),
        requests=HeadcountChangeRequestRepository(db, org),
        jobs=JobRepository(db, org),
        grades=GradeRepository(db, org),
        masters=MasterReferenceChecker(db, org),
        organizations=organizations,
        settings=settings,
        capacity=capacity,
        audit=audit,
    )


def get_position_assignment_service(
    capacity: Annotated[CapacityCalculator, Depends(get_capacity_calculator)],
    position_service: Annotated[PositionService, Depends(get_position_service)],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repository)],
    organizations: Annotated[OrganizationRepository, Depends(get_organization_repository)],
    audit: Annotated[AuditWriter, Depends(get_audit_writer)],
    db: DbSession,
    context: CurrentOrg,
) -> PositionAssignmentService:
    org = context.organization_id
    return PositionAssignmentService(
        assignments=PositionAssignmentRepository(db, org),
        positions=PositionRepository(db, org),
        employees=employees,
        organizations=organizations,
        position_service=position_service,
        capacity=capacity,
        audit=audit,
    )


def get_headcount_service(
    capacity: Annotated[CapacityCalculator, Depends(get_capacity_calculator)],
    organizations: Annotated[OrganizationRepository, Depends(get_organization_repository)],
    settings: Annotated[
        OrganizationSettingsRepository, Depends(get_organization_settings_repository)
    ],
    audit: Annotated[AuditWriter, Depends(get_audit_writer)],
    db: DbSession,
    context: CurrentOrg,
) -> HeadcountService:
    org = context.organization_id
    return HeadcountService(
        plans=HeadcountPlanRepository(db, org),
        requests=HeadcountChangeRequestRepository(db, org),
        reservations=HeadcountReservationRepository(db, org),
        positions=PositionRepository(db, org),
        assignments=PositionAssignmentRepository(db, org),
        jobs=JobRepository(db, org),
        masters=MasterReferenceChecker(db, org),
        organizations=organizations,
        settings=settings,
        capacity=capacity,
        audit=audit,
    )


def get_job_catalogue_service(
    organizations: Annotated[OrganizationRepository, Depends(get_organization_repository)],
    audit: Annotated[AuditWriter, Depends(get_audit_writer)],
    db: DbSession,
    context: CurrentOrg,
) -> JobCatalogueService:
    org = context.organization_id
    return JobCatalogueService(
        families=JobFamilyRepository(db, org),
        jobs=JobRepository(db, org),
        job_competencies=JobCompetencyRepository(db, org),
        positions=PositionRepository(db, org),
        assignments=PositionAssignmentRepository(db, org),
        grades=GradeRepository(db, org),
        masters=MasterReferenceChecker(db, org),
        organizations=organizations,
        audit=audit,
    )


def get_competency_service(
    lookups: Annotated[LookupRepository, Depends(get_lookup_repository)],
    organizations: Annotated[OrganizationRepository, Depends(get_organization_repository)],
    audit: Annotated[AuditWriter, Depends(get_audit_writer)],
    db: DbSession,
    context: CurrentOrg,
) -> CompetencyService:
    org = context.organization_id
    return CompetencyService(
        competencies=CompetencyRepository(db, org),
        job_competencies=JobCompetencyRepository(db, org),
        position_competencies=PositionCompetencyRepository(db, org),
        jobs=JobRepository(db, org),
        positions=PositionRepository(db, org),
        lookups=lookups,
        organizations=organizations,
        audit=audit,
    )


def get_schedule_service(
    organizations: Annotated[OrganizationRepository, Depends(get_organization_repository)],
    settings: Annotated[
        OrganizationSettingsRepository, Depends(get_organization_settings_repository)
    ],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repository)],
    branches: Annotated[BranchRepository, Depends(get_branch_repository)],
    audit: Annotated[AuditWriter, Depends(get_audit_writer)],
    db: DbSession,
    context: CurrentOrg,
) -> ScheduleService:
    org = context.organization_id
    return ScheduleService(
        patterns=WeeklyOffPatternRepository(db, org),
        calendars=HolidayCalendarRepository(db, org),
        holidays=HolidayRepository(db, org),
        assignments=BranchCalendarAssignmentRepository(db, org),
        branches=branches,
        employees=employees,
        branch_resolver=EmployeeBranchResolver(db, org),
        organizations=organizations,
        settings=settings,
        audit=audit,
        shift_assignments=ShiftAssignmentRepository(db, org),
        policy_versions=AttendancePolicyVersionRepository(db, org),
        shifts=ShiftRepository(db, org),
    )


def _record_engine(db: Session, org: uuid.UUID) -> RecordEngine:
    return RecordEngine(
        records=AttendanceRecordRepository(db, org),
        punches=PunchRepository(db, org),
        revisions=RevisionRepository(db, org),
        shifts=ShiftRepository(db, org),
        versions=AttendancePolicyVersionRepository(db, org),
    )


def get_attendance_config_service(
    organizations: Annotated[OrganizationRepository, Depends(get_organization_repository)],
    settings: Annotated[
        OrganizationSettingsRepository, Depends(get_organization_settings_repository)
    ],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repository)],
    schedule: Annotated[ScheduleService, Depends(get_schedule_service)],
    audit: Annotated[AuditWriter, Depends(get_audit_writer)],
    db: DbSession,
    context: CurrentOrg,
) -> AttendanceConfigService:
    org = context.organization_id
    return AttendanceConfigService(
        shifts=ShiftRepository(db, org),
        policies=AttendancePolicyRepository(db, org),
        versions=AttendancePolicyVersionRepository(db, org),
        assignments=ShiftAssignmentRepository(db, org),
        records=AttendanceRecordRepository(db, org),
        employees=employees,
        schedule=schedule,
        organizations=organizations,
        settings=settings,
        audit=audit,
    )


def get_attendance_service(
    organizations: Annotated[OrganizationRepository, Depends(get_organization_repository)],
    settings: Annotated[
        OrganizationSettingsRepository, Depends(get_organization_settings_repository)
    ],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repository)],
    schedule: Annotated[ScheduleService, Depends(get_schedule_service)],
    lookups: Annotated[LookupRepository, Depends(get_lookup_repository)],
    audit: Annotated[AuditWriter, Depends(get_audit_writer)],
    db: DbSession,
    context: CurrentOrg,
) -> AttendanceService:
    org = context.organization_id
    return AttendanceService(
        engine=_record_engine(db, org),
        records=AttendanceRecordRepository(db, org),
        punches=PunchRepository(db, org),
        revisions=RevisionRepository(db, org),
        employees=employees,
        schedule=schedule,
        lookups=lookups,
        organizations=organizations,
        settings=settings,
        audit=audit,
    )


def get_regularization_service(
    organizations: Annotated[OrganizationRepository, Depends(get_organization_repository)],
    settings: Annotated[
        OrganizationSettingsRepository, Depends(get_organization_settings_repository)
    ],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repository)],
    users: Annotated[UserRepository, Depends(get_user_repository)],
    access: Annotated[AccessService, Depends(get_access_service)],
    schedule: Annotated[ScheduleService, Depends(get_schedule_service)],
    audit: Annotated[AuditWriter, Depends(get_audit_writer)],
    db: DbSession,
    context: CurrentOrg,
) -> RegularizationService:
    org = context.organization_id
    return RegularizationService(
        engine=_record_engine(db, org),
        records=AttendanceRecordRepository(db, org),
        punches=PunchRepository(db, org),
        requests=RegularizationRequestRepository(db, org),
        items=RegularizationItemRepository(db, org),
        actions=RegularizationActionRepository(db, org),
        employees=employees,
        schedule=schedule,
        router=ApprovalRouter(employees=employees, users=users, access=access, organization_id=org),
        users=users,
        access=access,
        organizations=organizations,
        settings=settings,
        audit=audit,
    )


def get_leave_config_service(
    organizations: Annotated[OrganizationRepository, Depends(get_organization_repository)],
    settings: Annotated[
        OrganizationSettingsRepository, Depends(get_organization_settings_repository)
    ],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repository)],
    audit: Annotated[AuditWriter, Depends(get_audit_writer)],
    db: DbSession,
    context: CurrentOrg,
) -> LeaveConfigService:
    org = context.organization_id
    return LeaveConfigService(
        types=LeaveTypeRepository(db, org),
        policies=LeavePolicyRepository(db, org),
        versions=LeavePolicyVersionRepository(db, org),
        employees=employees,
        organizations=organizations,
        settings=settings,
        audit=audit,
    )


def get_leave_ledger_service(
    organizations: Annotated[OrganizationRepository, Depends(get_organization_repository)],
    settings: Annotated[
        OrganizationSettingsRepository, Depends(get_organization_settings_repository)
    ],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repository)],
    audit: Annotated[AuditWriter, Depends(get_audit_writer)],
    codec: Annotated[CursorCodec, Depends(get_cursor_codec)],
    db: DbSession,
    context: CurrentOrg,
) -> LeaveLedgerService:
    org = context.organization_id
    balances = LeaveBalanceRepository(db, org)
    transactions = LeaveTransactionRepository(db, org)
    return LeaveLedgerService(
        ledger=LeaveLedger(balances=balances, transactions=transactions),
        balances=balances,
        transactions=transactions,
        types=LeaveTypeRepository(db, org),
        employees=employees,
        organizations=organizations,
        settings=settings,
        audit=audit,
        codec=codec,
    )


def get_leave_application_service(
    organizations: Annotated[OrganizationRepository, Depends(get_organization_repository)],
    settings: Annotated[
        OrganizationSettingsRepository, Depends(get_organization_settings_repository)
    ],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repository)],
    users: Annotated[UserRepository, Depends(get_user_repository)],
    access: Annotated[AccessService, Depends(get_access_service)],
    config: Annotated[LeaveConfigService, Depends(get_leave_config_service)],
    schedule: Annotated[ScheduleService, Depends(get_schedule_service)],
    audit: Annotated[AuditWriter, Depends(get_audit_writer)],
    codec: Annotated[CursorCodec, Depends(get_cursor_codec)],
    db: DbSession,
    context: CurrentOrg,
) -> LeaveApplicationService:
    org = context.organization_id
    balances = LeaveBalanceRepository(db, org)
    return LeaveApplicationService(
        applications=LeaveApplicationRepository(db, org),
        slots=LeaveSlotRepository(db, org),
        actions=LeaveActionRepository(db, org),
        ledger=LeaveLedger(balances=balances, transactions=LeaveTransactionRepository(db, org)),
        balances=balances,
        config=config,
        types=LeaveTypeRepository(db, org),
        employees=employees,
        holidays=HolidayRepository(db, org),
        schedule=schedule,
        router=ApprovalRouter(employees=employees, users=users, access=access, organization_id=org),
        organizations=organizations,
        settings=settings,
        users=users,
        access=access,
        audit=audit,
        codec=codec,
        attendance=AttendanceLeaveEffects(
            engine=_record_engine(db, org), records=AttendanceRecordRepository(db, org)
        ),
    )


def get_template_service(
    organizations: Annotated[OrganizationRepository, Depends(get_organization_repository)],
    settings: Annotated[
        OrganizationSettingsRepository, Depends(get_organization_settings_repository)
    ],
    audit: Annotated[AuditWriter, Depends(get_audit_writer)],
    db: DbSession,
    context: CurrentOrg,
) -> TemplateService:
    org = context.organization_id
    return TemplateService(
        templates=TemplateRepository(db, org),
        versions=TemplateVersionRepository(db, org),
        tasks=TemplateTaskRepository(db, org),
        dependencies=TemplateDependencyRepository(db, org),
        categories=DocumentCategoryRepository(db, org),
        organizations=organizations,
        settings=settings,
        audit=audit,
    )


def get_onboarding_service(
    organizations: Annotated[OrganizationRepository, Depends(get_organization_repository)],
    settings: Annotated[
        OrganizationSettingsRepository, Depends(get_organization_settings_repository)
    ],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repository)],
    users: Annotated[UserRepository, Depends(get_user_repository)],
    access: Annotated[AccessService, Depends(get_access_service)],
    templates: Annotated[TemplateService, Depends(get_template_service)],
    audit: Annotated[AuditWriter, Depends(get_audit_writer)],
    db: DbSession,
    context: CurrentOrg,
) -> OnboardingService:
    org = context.organization_id
    return OnboardingService(
        instances=InstanceRepository(db, org),
        tasks=TaskRepository(db, org),
        task_dependencies=TaskDependencyRepository(db, org),
        instance_events=InstanceEventRepository(db, org),
        task_events=TaskEventRepository(db, org),
        task_documents=TaskDocumentRepository(db, org),
        acknowledgements=AcknowledgementRepository(db, org),
        submissions=SubmissionRepository(db, org),
        templates=templates,
        employees=employees,
        documents=EmployeeDocumentRepository(db, org),
        categories=DocumentCategoryRepository(db, org),
        users=users,
        access=access,
        organizations=organizations,
        settings=settings,
        audit=audit,
    )


def get_lifecycle_hooks(
    leave: Annotated[LeaveApplicationService, Depends(get_leave_application_service)],
    regularization: Annotated[RegularizationService, Depends(get_regularization_service)],
    onboarding: Annotated[OnboardingService, Depends(get_onboarding_service)],
) -> LifecycleHooks:
    return LifecycleHooks(leave=leave, regularization=regularization, onboarding=onboarding)


def get_employee_service(
    employees: Annotated[EmployeeRepository, Depends(get_employee_repository)],
    addresses: Annotated[EmployeeAddressRepository, Depends(get_employee_address_repository)],
    job_history: Annotated[
        EmployeeJobHistoryRepository, Depends(get_employee_job_history_repository)
    ],
    status_history: Annotated[
        EmployeeStatusHistoryRepository, Depends(get_employee_status_history_repository)
    ],
    organizations: Annotated[OrganizationRepository, Depends(get_organization_repository)],
    settings: Annotated[
        OrganizationSettingsRepository, Depends(get_organization_settings_repository)
    ],
    branch_references: Annotated[BranchRepository, Depends(get_branch_repository)],
    states: Annotated[StateRepository, Depends(get_state_repository)],
    codes: Annotated[EmployeeCodeService, Depends(get_employee_code_service)],
    identifiers: Annotated[IdentifierPolicyRepository, Depends(get_identifier_policy_repository)],
    custom_statuses: Annotated[CustomStatusRepository, Depends(get_custom_status_repository)],
    registry: Annotated[EmployeeCodeRegistryRepository, Depends(get_code_registry_repository)],
    identifier_values: Annotated[EmployeeIdentifierRepository, Depends(get_identifier_repository)],
    cursor_codec: Annotated[CursorCodec, Depends(get_cursor_codec)],
    salary: Annotated[SalaryService, Depends(get_salary_service)],
    position_assignments: Annotated[
        PositionAssignmentService, Depends(get_position_assignment_service)
    ],
    audit: Annotated[AuditWriter, Depends(get_audit_writer)],
    lifecycle: Annotated[LifecycleHooks, Depends(get_lifecycle_hooks)],
    db: DbSession,
    context: CurrentOrg,
) -> EmployeeService:
    return EmployeeService(
        lifecycle=lifecycle,
        employees=employees,
        addresses=addresses,
        job_history=job_history,
        status_history=status_history,
        organizations=organizations,
        settings=settings,
        branch_references=branch_references,
        masters=MasterReferenceChecker(db, context.organization_id),
        states=states,
        codes=codes,
        identifiers=identifiers,
        custom_statuses=custom_statuses,
        registry=registry,
        identifier_values=identifier_values,
        cursor_codec=cursor_codec,
        salary=salary,
        position_assignments=position_assignments,
        audit=audit,
    )


def get_employee_code_service(
    rules: Annotated[IdRuleRepository, Depends(get_id_rule_repository)],
    sequences: Annotated[EmployeeIdSequenceRepository, Depends(get_id_sequence_repository)],
    registry: Annotated[EmployeeCodeRegistryRepository, Depends(get_code_registry_repository)],
    organizations: Annotated[OrganizationRepository, Depends(get_organization_repository)],
    settings: Annotated[
        OrganizationSettingsRepository, Depends(get_organization_settings_repository)
    ],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repository)],
    audit: Annotated[AuditWriter, Depends(get_audit_writer)],
    db: DbSession,
    context: CurrentOrg,
) -> EmployeeCodeService:
    return EmployeeCodeService(
        rules=rules,
        sequences=sequences,
        registry=registry,
        organizations=organizations,
        settings=settings,
        departments=DepartmentRepository(db, context.organization_id),
        employees=employees,
        audit=audit,
    )


def get_catalogue_service(
    branches: Annotated[BranchRepository, Depends(get_branch_repository)],
    lookups: Annotated[LookupRepository, Depends(get_lookup_repository)],
    custom_statuses: Annotated[CustomStatusRepository, Depends(get_custom_status_repository)],
    registrations: Annotated[
        OrganizationRegistrationRepository, Depends(get_registration_repository)
    ],
    organizations: Annotated[OrganizationRepository, Depends(get_organization_repository)],
    states: Annotated[StateRepository, Depends(get_state_repository)],
    employees: Annotated[EmployeeRepository, Depends(get_employee_repository)],
    id_rules: Annotated[IdRuleRepository, Depends(get_id_rule_repository)],
    audit: Annotated[AuditWriter, Depends(get_audit_writer)],
    db: DbSession,
    context: CurrentOrg,
) -> CatalogueService:
    return CatalogueService(
        branches=branches,
        masters=DepartmentRepository(db, context.organization_id),
        lookups=lookups,
        custom_statuses=custom_statuses,
        registrations=registrations,
        organizations=organizations,
        states=states,
        employees=employees,
        id_rules=id_rules,
        audit=audit,
    )


def get_organization_service(
    organizations: Annotated[OrganizationRepository, Depends(get_organization_repository)],
    settings: Annotated[
        OrganizationSettingsRepository, Depends(get_organization_settings_repository)
    ],
    groups: Annotated[OrganizationGroupRepository, Depends(get_organization_group_repository)],
    states: Annotated[StateRepository, Depends(get_state_repository)],
    audit: Annotated[AuditWriter, Depends(get_audit_writer)],
) -> OrganizationService:
    return OrganizationService(
        organizations=organizations,
        settings=settings,
        groups=groups,
        states=states,
        audit=audit,
    )


def get_org_config_service(
    registrations: Annotated[
        OrganizationRegistrationRepository, Depends(get_registration_repository)
    ],
    policies: Annotated[IdentifierPolicyRepository, Depends(get_identifier_policy_repository)],
    nomination_configs: Annotated[
        NominationConfigRepository, Depends(get_nomination_config_repository)
    ],
    organizations: Annotated[OrganizationRepository, Depends(get_organization_repository)],
    branches: Annotated[BranchRepository, Depends(get_branch_repository)],
    states: Annotated[StateRepository, Depends(get_state_repository)],
    audit: Annotated[AuditWriter, Depends(get_audit_writer)],
) -> OrganizationConfigService:
    return OrganizationConfigService(
        registrations=registrations,
        policies=policies,
        nomination_configs=nomination_configs,
        organizations=organizations,
        branches=branches,
        states=states,
        audit=audit,
    )


def get_access_service(
    users: Annotated[UserRepository, Depends(get_user_repository)],
    grants: Annotated[AccessGrantRepository, Depends(get_access_grant_repository)],
    overrides: Annotated[OrganizationRolePermissionRepository, Depends(get_override_repository)],
    refresh_tokens: Annotated[RefreshTokenRepository, Depends(get_refresh_token_repository)],
    audit: Annotated[AuditWriter, Depends(get_audit_writer)],
    organizations: Annotated[OrganizationRepository, Depends(get_organization_repository)],
    cache: Annotated[PermissionCache, Depends(get_permission_cache)],
) -> AccessService:
    return AccessService(
        users=users,
        grants=grants,
        overrides=overrides,
        refresh_tokens=refresh_tokens,
        organizations=organizations,
        audit=audit,
        cache=cache,
    )


# --------------------------------------------------------------------------- caller


def get_request_metadata(request: Request) -> RequestMetadata:
    """Correlation and provenance for audit rows (§20.1).

    The client IP is taken from the connection, not from ``X-Forwarded-For``: that header is
    client-controlled unless a trusted proxy rewrites it, and an audit record built from
    attacker-supplied provenance is worse than one with no IP at all. A deployment behind a
    proxy should configure the ASGI server's own forwarded-header handling, which rewrites
    ``request.client`` for us.
    """
    return RequestMetadata(
        request_id=current_request_id(),
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    users: Annotated[UserRepository, Depends(get_user_repository)],
    verifier: Annotated[TokenVerifier, Depends(get_token_verifier)],
    metadata: Annotated[RequestMetadata, Depends(get_request_metadata)],
) -> ActorContext:
    """Verify the bearer token and load the account behind it.

    The account is re-read on every request and checked for being active, so deactivating a
    user takes effect on their next call rather than when their access token expires (§25.5).
    Roles and permissions are **not** read from the token — see
    :class:`app.core.security.TokenClaims`.
    """
    if credentials is None or not credentials.credentials:
        raise Unauthenticated()

    claims = verifier.verify(credentials.credentials)
    user = users.get(claims.subject)
    if user is None or not user.is_active:
        # Same error as a bad token: whether a given subject exists is not a client's business.
        raise Unauthenticated("The access token is invalid or has expired.")

    bind_request_context(
        request_id=metadata.request_id or "",
        actor_id=str(user.id),
    )
    return ActorContext(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        employee_id=user.employee_id,
        is_platform_admin=False,
        metadata=metadata,
    )


CurrentUser = Annotated[ActorContext, Depends(get_current_user)]


# --------------------------------------------------------------------------- organization


def get_org_context(
    org_id: Annotated[uuid.UUID, Path(description="Organization the request is scoped to")],
    actor: CurrentUser,
    access: Annotated[AccessService, Depends(get_access_service)],
) -> OrgContext:
    """Bind the request to one organization the caller may actually access (§6.1 layer 1).

    The organization comes from the **path** only. Resolution raises
    ``403 ORG_ACCESS_DENIED`` when the caller holds no active grant there, which §24.3
    distinguishes from ``PERMISSION_DENIED``.
    """
    resolved = access.resolve(actor.user_id, org_id)
    bind_request_context(
        request_id=actor.metadata.request_id or "",
        actor_id=str(actor.user_id),
        organization_id=str(org_id),
    )
    return actor.for_organization(
        org_id,
        permissions=resolved.permissions,
        via_break_glass=resolved.via_break_glass,
    )


CurrentOrg = Annotated[OrgContext, Depends(get_org_context)]


def require(*permissions: Permission) -> Callable[[OrgContext], OrgContext]:
    """Build a dependency asserting the caller holds **all** of ``permissions``.

    Used as ``context: OrgContext = Depends(require(Permission.EMPLOYEE_VIEW))``, so the
    requirement is declared in the signature and visible in the OpenAPI document rather than
    buried in the handler body.
    """

    def dependency(context: CurrentOrg) -> OrgContext:
        for permission in permissions:
            context.require(permission)
        return context

    return dependency


def require_any(*permissions: Permission) -> Callable[[OrgContext], OrgContext]:
    """As :func:`require`, but any one of the permissions suffices.

    §24.4.5 needs this: listing documents is allowed with ``document.download`` *or*
    ``identity.view_masked`` *or* ``employee.edit``.
    """

    def dependency(context: CurrentOrg) -> OrgContext:
        context.require_any(*permissions)
        return context

    return dependency


# --------------------------------------------------------------------------- headers


def get_if_match(
    if_match: Annotated[
        str | None,
        Header(
            alias="If-Match",
            description="The ETag from a prior GET. Required on updates (§24.1).",
        ),
    ] = None,
) -> int | None:
    """Parse ``If-Match`` into a row version, or ``None`` when absent."""
    return parse_if_match(if_match)


IfMatch = Annotated[int | None, Depends(get_if_match)]


def get_idempotency_key(
    idempotency_key: Annotated[
        uuid.UUID | None,
        Header(
            alias="Idempotency-Key",
            description="UUID making a create request safely retryable (§24.1).",
        ),
    ] = None,
) -> uuid.UUID | None:
    return idempotency_key


IdempotencyKeyHeader = Annotated[uuid.UUID | None, Depends(get_idempotency_key)]


__all__ = [
    "CurrentOrg",
    "CurrentUser",
    "DbSession",
    "IdempotencyKeyHeader",
    "IfMatch",
    "get_access_service",
    "get_app_settings",
    "get_assignment_component_repository",
    "get_audit_log_repository",
    "get_audit_query_service",
    "get_audit_writer",
    "get_auth_service",
    "get_bank_account_repository",
    "get_bank_account_service",
    "get_blind_indexer",
    "get_branch_repository",
    "get_catalogue_service",
    "get_code_registry_repository",
    "get_current_user",
    "get_cursor_codec",
    "get_custom_status_repository",
    "get_db",
    "get_document_category_repository",
    "get_document_mime_repository",
    "get_document_service",
    "get_education_repository",
    "get_employee_address_repository",
    "get_employee_code_service",
    "get_employee_document_repository",
    "get_employee_job_history_repository",
    "get_employee_repository",
    "get_employee_service",
    "get_employee_status_history_repository",
    "get_erasure_repository",
    "get_family_repository",
    "get_field_cipher",
    "get_id_rule_repository",
    "get_id_sequence_repository",
    "get_identifier_policy_repository",
    "get_identifier_repository",
    "get_identifier_service",
    "get_if_match",
    "get_key_provider",
    "get_local_storage",
    "get_lookup_repository",
    "get_nomination_config_repository",
    "get_nomination_repository",
    "get_org_config_service",
    "get_org_context",
    "get_organization_group_repository",
    "get_organization_repository",
    "get_organization_settings_repository",
    "get_permission_cache",
    "get_previous_employment_document_repository",
    "get_previous_employment_repository",
    "get_privacy_notice_repository",
    "get_privacy_service",
    "get_profile_record_service",
    "get_request_metadata",
    "get_retention_batch_repository",
    "get_salary_assignment_repository",
    "get_salary_component_repository",
    "get_salary_history_repository",
    "get_salary_mapping_repository",
    "get_salary_service",
    "get_salary_template_repository",
    "get_salary_version_repository",
    "get_token_issuer",
    "get_token_verifier",
    "get_transfer_service",
    "get_url_signer",
    "require",
    "require_any",
    "reset_permission_cache",
]


def get_submission_service(
    onboarding: Annotated[OnboardingService, Depends(get_onboarding_service)],
    employee_service: Annotated[EmployeeService, Depends(get_employee_service)],
    profile: Annotated[ProfileRecordService, Depends(get_profile_record_service)],
    cipher: Annotated[FieldCipher, Depends(get_field_cipher)],
    audit: Annotated[AuditWriter, Depends(get_audit_writer)],
    db: DbSession,
    context: CurrentOrg,
) -> SubmissionService:
    return SubmissionService(
        submissions=SubmissionRepository(db, context.organization_id),
        onboarding=onboarding,
        employee_service=employee_service,
        profile=profile,
        cipher=cipher,
        audit=audit,
    )
