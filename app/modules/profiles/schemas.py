"""Pydantic schemas for onboarding and profile updates."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ProfileInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OnboardRequest(ProfileInput):
    display_name: str = Field(min_length=1, max_length=150)
    phone: str | None = Field(default=None, max_length=32)
    requested_role: Literal["patient", "doctor", "pharmacist", "lab_staff"]
    date_of_birth: date | None = None
    sex: Literal["female", "male"] | None = None

    @model_validator(mode="after")
    def validate_patient_fields(self):
        if self.requested_role == "patient" and (self.date_of_birth is None or self.sex is None):
            raise ValueError("Patient onboarding requires date_of_birth and sex.")
        if self.date_of_birth is not None and self.date_of_birth > date.today():
            raise ValueError("date_of_birth cannot be in the future.")
        if self.requested_role != "patient" and (
            self.date_of_birth is not None or self.sex is not None
        ):
            raise ValueError("Patient fields are only accepted for patient onboarding.")
        return self


class AccountUpdateRequest(ProfileInput):
    display_name: str | None = Field(default=None, min_length=1, max_length=150)
    phone: str | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def reject_empty(self):
        if not self.model_fields_set:
            raise ValueError("At least one field is required.")
        return self

    @field_validator("display_name")
    @classmethod
    def check_display_name(cls, value):
        if value is None:
            raise ValueError("Display name cannot be null.")
        return value


class NotificationPreferencesUpdate(ProfileInput):
    medication_in_app: bool | None = Field(default=None, strict=True)
    vaccination_in_app: bool | None = Field(default=None, strict=True)

    @model_validator(mode="after")
    def reject_empty(self):
        if not self.model_fields_set:
            raise ValueError("At least one preference is required.")
        return self

    @field_validator("medication_in_app", "vaccination_in_app")
    @classmethod
    def check_preference(cls, value):
        if value is None:
            raise ValueError("Notification preference cannot be null.")
        return value


class PatientProfileUpdateRequest(ProfileInput):
    date_of_birth: date | None = None
    sex: Literal["female", "male"] | None = None
    emergency_contact_name: str | None = Field(default=None, max_length=150)
    emergency_contact_phone: str | None = Field(default=None, max_length=32)
    notification_preferences: NotificationPreferencesUpdate | None = None

    @model_validator(mode="after")
    def reject_empty(self):
        if not self.model_fields_set:
            raise ValueError("At least one field is required.")
        return self

    @field_validator("date_of_birth", "sex", "notification_preferences")
    @classmethod
    def check_required_values(cls, value):
        if value is None:
            raise ValueError("This field cannot be null.")
        return value

    @field_validator("date_of_birth")
    @classmethod
    def check_date_of_birth(cls, value):
        if value is not None and value > date.today():
            raise ValueError("date_of_birth cannot be in the future.")
        return value


class UserResponse(BaseModel):
    id: UUID
    email: str | None = None
    display_name: str
    phone: str | None = None
    status: str
    created_at: datetime
    updated_at: datetime


class PatientProfileResponse(BaseModel):
    user_id: UUID
    date_of_birth: date
    sex: str
    emergency_contact_name: str | None = None
    emergency_contact_phone: str | None = None
    notification_preferences: dict[str, bool]
    created_at: datetime
    updated_at: datetime


class ProfessionalProfileResponse(BaseModel):
    user_id: UUID
    role: str
    doctor_reference: str | None = None
    registration_number: str | None = None
    qualifications: str | None = None
    specialty: str | None = None
    verification_status: str
    created_at: datetime
    updated_at: datetime


class MembershipResponse(BaseModel):
    id: UUID
    organization_id: UUID
    user_id: UUID
    role: str
    status: str
    verified_at: datetime | None = None
    created_at: datetime


class MeResponse(BaseModel):
    user: UserResponse
    roles: list[str]
    memberships: list[MembershipResponse]
    patient_profile: PatientProfileResponse | None = None
    professional_profile: ProfessionalProfileResponse | None = None
