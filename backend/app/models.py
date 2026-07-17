from typing import Literal

from pydantic import BaseModel, Field, model_validator


class Keyword(BaseModel):
    term: str = Field(min_length=1, max_length=120)
    category: Literal["required", "preferred", "responsibility", "technology"]
    importance: Literal["high", "medium", "low"]
    evidence: str = Field(min_length=1, max_length=500)


class ExtractKeywordsRequest(BaseModel):
    job_text: str = Field(min_length=50)


class ExtractKeywordsResponse(BaseModel):
    company: str = Field(default="Company", max_length=120)
    role: str = Field(default="Role", max_length=160)
    keywords: list[Keyword]


class EditTarget(BaseModel):
    section: Literal["Experience", "Projects", "Technologies"]
    anchor: str = Field(min_length=1, max_length=200)
    item_index: int | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def validate_shape(self):
        if self.section == "Technologies" and self.item_index is not None:
            raise ValueError("technology edits cannot have an item_index")
        if self.section != "Technologies" and self.item_index is None:
            raise ValueError("bullet edits require an item_index")
        return self


class ProposedEdit(BaseModel):
    target: EditTarget
    new_text: str = Field(min_length=1, max_length=2000)
    reason: str = Field(min_length=1, max_length=500)


class ProposedEditsResponse(BaseModel):
    edits: list[ProposedEdit] = Field(default_factory=list, max_length=12)


class ReviewedEdit(ProposedEdit):
    original_text: str
    traceable: bool
    issues: list[str] = Field(default_factory=list)


class GenerateDiffRequest(BaseModel):
    job_text: str = Field(min_length=50)
    keywords: list[Keyword] = Field(min_length=1, max_length=100)


class GenerateDiffResponse(BaseModel):
    edits: list[ReviewedEdit]


class CompileRequest(BaseModel):
    company: str = Field(default="Company", max_length=120)
    role: str = Field(default="Role", max_length=160)
    approved_edits: list[ProposedEdit] = Field(default_factory=list, max_length=50)
    keywords: list[Keyword] = Field(default_factory=list, max_length=100)
