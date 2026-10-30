"""Education, previous employment, family and nomination contracts (§12-§13)."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.models.profile import ResultType
from app.schemas.common import ApiModel


class EducationCreateRequest(ApiModel):
    qualification_level: Annotated[str, Field(max_length=50)]
    degree_name: Annotated[str, Field(max_length=150)] | None = None
    specialization: Annotated[str, Field(max_length=150)] | None = None
    institution_name: Annotated[str, Field(min_length=2, max_length=200)]
    board_or_university: Annotated[str, Field(max_length=200)] | None = None
    study_mode: Annotated[str, Field(max_length=50)] | None = None
    is_ongoing: bool = False
    start_year: Annotated[int, Field(ge=1940, le=2200)] | None = None
    passing_year: Annotated[int, Field(ge=1940, le=2200)] | None = None
    result_type: ResultType | None = None
    result_value: Annotated[str, Field(max_length=20)] | None = None
    maximum_result_value: Annotated[float, Field(gt=0, le=100)] | None = None
    certificate_document_id: uuid.UUID | None = None
    remarks: Annotated[str, Field(max_length=500)] | None = None

    @model_validator(mode="after")
    def validate_result(self) -> EducationCreateRequest:
        if (
            self.qualification_level not in {"SECONDARY_10", "HIGHER_SECONDARY_12"}
            and not self.degree_name
        ):
            raise ValueError("degree_name is required for this qualification level")
        if not self.is_ongoing and self.passing_year is None:
            raise ValueError("passing_year is required unless the course is ongoing")
        if self.start_year and self.passing_year and self.start_year > self.passing_year:
            raise ValueError("start_year must not exceed passing_year")
        if self.result_type is not None and not self.result_value:
            raise ValueError("result_value is required when result_type is set")
        if self.result_type is ResultType.CGPA and self.maximum_result_value is None:
            raise ValueError("maximum_result_value is required for a CGPA")
        if self.result_type in {ResultType.GRADE, ResultType.OTHER} and (
            self.maximum_result_value is not None
        ):
            raise ValueError("maximum_result_value is only valid for CGPA or PERCENTAGE")
        return self


class EducationUpdateRequest(ApiModel):
    qualification_level: Annotated[str, Field(max_length=50)] | None = None
    degree_name: Annotated[str, Field(max_length=150)] | None = None
    specialization: Annotated[str, Field(max_length=150)] | None = None
    institution_name: Annotated[str, Field(min_length=2, max_length=200)] | None = None
    board_or_university: Annotated[str, Field(max_length=200)] | None = None
    study_mode: Annotated[str, Field(max_length=50)] | None = None
    is_ongoing: bool | None = None
    start_year: Annotated[int, Field(ge=1940, le=2200)] | None = None
    passing_year: Annotated[int, Field(ge=1940, le=2200)] | None = None
    result_type: ResultType | None = None
    result_value: Annotated[str, Field(max_length=20)] | None = None
    maximum_result_value: Annotated[float, Field(gt=0, le=100)] | None = None
    certificate_document_id: uuid.UUID | None = None
    remarks: Annotated[str, Field(max_length=500)] | None = None


class EducationResponse(ApiModel):
    id: uuid.UUID
    qualification_level: str
    degree_name: str | None
    specialization: str | None
    institution_name: str
    board_or_university: str | None
    study_mode: str | None
    is_ongoing: bool
    start_year: int | None
    passing_year: int | None
    result_type: str | None
    result_value: str | None
    maximum_result_value: Decimal | None
    certificate_document_id: uuid.UUID | None
    remarks: str | None
    row_version: int


class PreviousEmploymentCreateRequest(ApiModel):
    employer_name: Annotated[str, Field(min_length=2, max_length=200)]
    designation: Annotated[str, Field(max_length=150)] | None = None
    employment_start_date: date
    employment_end_date: date
    last_drawn_ctc: Annotated[Decimal, Field(ge=0)] | None = None
    last_drawn_ctc_currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None = None
    reason_for_leaving: Annotated[str, Field(max_length=500)] | None = None
    reference_name: Annotated[str, Field(max_length=150)] | None = None
    reference_contact: Annotated[str, Field(max_length=254)] | None = None
    experience_letter_document_id: uuid.UUID | None = None
    relieving_letter_document_id: uuid.UUID | None = None
    background_verification_status: Annotated[str, Field(max_length=30)] | None = None
    background_verification_date: date | None = None
    payslip_document_ids: list[uuid.UUID] = Field(default_factory=list)
    remarks: Annotated[str, Field(max_length=500)] | None = None

    @model_validator(mode="after")
    def validate_interval(self) -> PreviousEmploymentCreateRequest:
        if self.employment_end_date < self.employment_start_date:
            raise ValueError("employment_end_date must not precede employment_start_date")
        return self


class PreviousEmploymentUpdateRequest(ApiModel):
    employer_name: Annotated[str, Field(min_length=2, max_length=200)] | None = None
    designation: Annotated[str, Field(max_length=150)] | None = None
    employment_start_date: date | None = None
    employment_end_date: date | None = None
    last_drawn_ctc: Annotated[Decimal, Field(ge=0)] | None = None
    last_drawn_ctc_currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None = None
    reason_for_leaving: Annotated[str, Field(max_length=500)] | None = None
    reference_name: Annotated[str, Field(max_length=150)] | None = None
    reference_contact: Annotated[str, Field(max_length=254)] | None = None
    experience_letter_document_id: uuid.UUID | None = None
    relieving_letter_document_id: uuid.UUID | None = None
    background_verification_status: Annotated[str, Field(max_length=30)] | None = None
    background_verification_date: date | None = None
    payslip_document_ids: list[uuid.UUID] | None = None
    remarks: Annotated[str, Field(max_length=500)] | None = None


class PreviousEmploymentResponse(ApiModel):
    id: uuid.UUID
    employer_name: str
    designation: str | None
    employment_start_date: date
    employment_end_date: date
    last_drawn_ctc: Decimal | None = None
    last_drawn_ctc_currency: str | None = None
    reason_for_leaving: str | None
    reference_name: str | None
    reference_contact: str | None
    experience_letter_document_id: uuid.UUID | None
    relieving_letter_document_id: uuid.UUID | None
    background_verification_status: str | None = None
    background_verification_date: date | None = None
    overlap_review_status: str
    payslip_document_ids: list[uuid.UUID] = Field(default_factory=list)
    remarks: str | None
    row_version: int


class OverlapReviewRequest(ApiModel):
    decision: Literal["ACCEPTED"]
    remarks: Annotated[str, Field(min_length=10, max_length=500)] | None = None


class FamilyCreateRequest(ApiModel):
    full_name: Annotated[str, Field(min_length=1, max_length=150)]
    relationship_code: Annotated[str, Field(max_length=50)]
    date_of_birth: date | None = None
    occupation: Annotated[str, Field(max_length=100)] | None = None
    is_dependent: bool = False
    insurance_coverage_reference: Annotated[str, Field(max_length=100)] | None = None
    remarks: Annotated[str, Field(max_length=500)] | None = None


class FamilyUpdateRequest(ApiModel):
    full_name: Annotated[str, Field(min_length=1, max_length=150)] | None = None
    relationship_code: Annotated[str, Field(max_length=50)] | None = None
    date_of_birth: date | None = None
    occupation: Annotated[str, Field(max_length=100)] | None = None
    is_dependent: bool | None = None
    insurance_coverage_reference: Annotated[str, Field(max_length=100)] | None = None
    remarks: Annotated[str, Field(max_length=500)] | None = None


class FamilyResponse(FamilyCreateRequest):
    id: uuid.UUID
    row_version: int


class NomineeInput(ApiModel):
    nominee_name: Annotated[str, Field(min_length=1, max_length=150)]
    relationship_code: Annotated[str, Field(max_length=50)]
    date_of_birth: date | None = None
    contact_number: Annotated[str, Field(max_length=16)] | None = None
    address: Annotated[str, Field(max_length=500)] | None = None
    share_percentage: Annotated[Decimal, Field(gt=0, le=100)]
    family_member_id: uuid.UUID | None = None
    guardian_name: Annotated[str, Field(max_length=150)] | None = None
    guardian_relationship: Annotated[str, Field(max_length=100)] | None = None
    supporting_document_id: uuid.UUID | None = None


class NominationReplaceRequest(ApiModel):
    effective_from: date
    nomination_label: Annotated[str, Field(max_length=100)] | None = None
    nominees: Annotated[list[NomineeInput], Field(min_length=1, max_length=20)]


class NomineeResponse(NomineeInput):
    id: uuid.UUID
    effective_from: date
    effective_to: date | None
    nomination_label: str | None


class NominationSetResponse(ApiModel):
    nomination_type: str
    effective_from: date
    effective_to: date | None
    nomination_label: str | None
    nominees: list[NomineeResponse]
    total_share: Decimal


__all__ = [
    "EducationCreateRequest",
    "EducationResponse",
    "EducationUpdateRequest",
    "FamilyCreateRequest",
    "FamilyResponse",
    "FamilyUpdateRequest",
    "NominationReplaceRequest",
    "NominationSetResponse",
    "NomineeInput",
    "NomineeResponse",
    "OverlapReviewRequest",
    "PreviousEmploymentCreateRequest",
    "PreviousEmploymentResponse",
    "PreviousEmploymentUpdateRequest",
]
