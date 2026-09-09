from pydantic import BaseModel, ConfigDict, Field, field_validator


class DocumentationPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    body: str
    source_path: str
    source_url: str


class DocumentationChunk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    title: str
    section: str
    text: str
    source_path: str
    source_url: str


class GroundedDocumentationAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=1)
    citations: list[str] = Field(default_factory=list)

    @field_validator("citations")
    @classmethod
    def unique_citations(cls, citations: list[str]) -> list[str]:
        return list(dict.fromkeys(citations))
